# -*- coding: utf-8 -*-
# MegEngine is Licensed under the Apache License, Version 2.0 (the "License")
#
# Copyright (c) 2014-2021 Megvii Inc. All rights reserved.
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT ARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
import builtins
import collections
import copy
import fnmatch
import functools
import keyword
import re
import weakref
from inspect import getcallargs, getmembers, isclass, ismethod
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Type, Union

from ... import functional as F
from ... import get_logger
from ... import module as M
from ...core._imperative_rt.core2 import Tensor as RawTensor
from ...core._imperative_rt.core2 import (
    is_tracing_module,
    set_module_tracing,
    unset_module_tracing,
)
from ...core._trace_option import set_symbolic_shape
from ...core.tensor.array_method import ArrayMethodMixin
from ...module import Module
from ...quantization.fake_quant import LSQ, TQT, FakeQuantize
from ...quantization.observer import (
    ExponentialMovingAverageObserver,
    MinMaxObserver,
    SyncMinMaxObserver,
)
from ...tensor import Tensor
from .expr import Apply, CallFunction, CallMethod, Constant, Expr, GetAttr, Input
from .module_tracer import (
    Patcher,
    active_module_tracer,
    module_tracer,
    set_active_module_tracer,
)
from .node import ModuleNode, Node, NodeMixin, TensorNode
from .pytree import ArgsIndex, tree_flatten

logger = get_logger(__name__)


def _is_builtin_name(name: str) -> bool:
    return (
        name in builtins.__dict__
        or name in keyword.kwlist
        or name in {"inf", "nan", "NoneType"}
    )


def _is_leaf(node):
    assert isinstance(node, RawTensor), "doesn't support {} in return values".format(
        type(node)
    )
    return isinstance(node, RawTensor)


def wrap_tensors(tensors: Tensor, nodes: TensorNode):
    inp_tensors = copy.deepcopy(tensors)
    inp_tensors, inp_def_v = tree_flatten(inp_tensors)
    inp_nodes, inp_def_n = tree_flatten(nodes)
    for v, n in zip(inp_tensors, inp_nodes):
        if isinstance(n, TensorNode) and isinstance(v, Tensor):
            NodeMixin.wrap_safe(v, n)
    return inp_def_v.unflatten(inp_tensors)


class _InsertExprs:
    def __init__(self, graph, expr: Optional[Expr] = None, after: bool = True):
        self.graph = graph
        self.global_scope = InternalGraph()
        self.global_scope._used_names.update(graph._used_names)
        self.expr = expr
        self.after = after

    def __enter__(self):
        self.use_sym_shape = set_symbolic_shape(True)
        set_module_tracing()
        assert active_module_tracer() is None
        set_active_module_tracer(module_tracer(_wrapped_function))
        active_module_tracer().patcher.__enter__()
        active_module_tracer().push_scope(self.global_scope)

    def __exit__(self, ty, va, tr):
        set_symbolic_shape(self.use_sym_shape)
        unset_module_tracing()
        active_module_tracer().patcher.__exit__(ty, va, tr)
        set_active_module_tracer(None)
        index = len(self.graph._exprs) if self.after else 0
        if self.expr is not None:
            index = self.graph._exprs.index(self.expr)
        if self.after:
            index += 1
        for expr in self.global_scope._exprs:
            self.graph._exprs.insert(index, expr)
            index += 1
        self.graph._used_names.update(self.global_scope._used_names)


class InternalGraph:
    """
    ``InternalGraph`` is a graph consist of ``Node`` and  ``Expr``, it is used to represent the execution procedure of Module's forward method.

    Attributes:
    _exprs: List of Exprs in order of execution
    _inputs: Input Nodes of InternalGraph
    _outputs: Output Nodes of InternalGraph
    """

    _exprs = None  # type: List[Expr]
    _inputs = None  # type: List[Node]
    _outputs = None  # type: List[Node]

    def __init__(self, name: str = None, prefix_name: str = ""):
        self._exprs = []
        self._inputs = []
        self._outputs = []
        self._watch_point = []
        self._end_point = []
        self._used_names = {}
        self._rst = collections.defaultdict(list)
        self._name = name
        self._prefix_name = prefix_name

    def insert(self, expr):
        self._exprs.append(expr)

    def _create_unique_name(self, name: str) -> str:
        assert isinstance(name, str)
        name = re.sub("[^0-9a-zA-Z_]+", "_", name)
        if name[0].isdigit():
            name = "_{}".format(name)

        while name in self._used_names or _is_builtin_name(name):
            match = re.match(r"(.*)_(\d+)$", name)
            if match is None:
                name = name + "_1"
            else:
                base, num = match.group(1, 2)
                name = "{}_{}".format(base, int(num) + 1)

        self._used_names.setdefault(name)
        return name

    @property
    def inputs(self):
        return self._inputs

    @property
    def outputs(self):
        return self._outputs

    @property
    def expr_filter(self):
        return ExprFilter(_expr_iter(self))

    @property
    def node_filter(self):
        return NodeFilter(_node_iter(self))

    def get_function_by_type(self, func: Callable = None):
        return self.expr_filter.call_function(func)

    def get_method_by_type(self, method: str = None):
        return self.expr_filter.call_method(method)

    def get_expr_by_id(self, expr_id: List[int] = None):
        return self.expr_filter.expr_id(expr_id)

    def get_module_by_type(self, module_cls: Module):
        assert issubclass(module_cls, Module)
        return self.node_filter.type(module_cls, ModuleNode)

    def get_node_by_id(self, node_id: List[int] = None):
        return self.node_filter.node_id(node_id)

    def get_node_by_name(self, name: str = None, ignorecase: bool = True):
        return self.node_filter.name(name, ignorecase)

    def add_input(self, i):
        self._inputs.append(i)

    def add_output(self, o):
        self._outputs.append(o)

    def _replace_inputs_outputs_and_add_prefixname(self, repl_dict, prefix_name=""):

        for node, repl_node in repl_dict.items():
            assert node in self._inputs or node in self._outputs
            for i in node.users:
                if i not in repl_node.users:
                    repl_node.users.append(i)

        for idx, i in enumerate(self._inputs):
            if i in repl_dict:
                self._inputs[idx] = repl_dict[i]

        for idx, o in enumerate(self._outputs):
            if o in repl_dict:
                self._outputs[idx] = repl_dict[o]

        for expr in self._exprs:

            for idx, i in enumerate(expr.inputs):
                assert i._name is not None
                if i in repl_dict:
                    expr.inputs[idx] = repl_dict[i]
                elif isinstance(i, TensorNode) and prefix_name not in i._name:
                    if i.top_graph != active_module_tracer().current_scope():
                        i._name = (
                            active_module_tracer()
                            .current_scope()
                            ._create_unique_name(prefix_name + i._name.lstrip("_"))
                        )

            for idx, o in enumerate(expr.outputs):
                assert o._name is not None
                if o in repl_dict:
                    expr.outputs[idx] = repl_dict[o]
                    expr.outputs[idx].expr = expr
                elif isinstance(o, TensorNode) and prefix_name not in i._name:
                    if o.top_graph != active_module_tracer().current_scope():
                        o._name = (
                            active_module_tracer()
                            .current_scope()
                            ._create_unique_name(prefix_name + o._name.lstrip("_"))
                        )

    def get_dep_exprs(self, nodes: Sequence[Node]) -> List[Expr]:
        if not isinstance(nodes, Sequence):
            nodes = (nodes,)
        ret = list()
        queue = list(nodes)
        visited_queue = list()
        while queue:
            node = queue.pop()
            visited_queue.append(node)

            expr = node.expr

            if expr not in ret:
                ret.append(expr)

            for i in expr.inputs:
                if i not in queue and i not in visited_queue:
                    queue.append(i)
        return ret

    def reset_inputs(self, *args, **kwargs):
        forma_mnode = self.inputs[0]
        actual_mnodes = forma_mnode.actual_mnode
        call_nodes = []
        for n in actual_mnodes:
            for c_expr in n.users:
                if isinstance(c_expr, CallMethod) and c_expr.method == "__call__":
                    call_nodes.append((c_expr, n))

        moudle = forma_mnode.owner
        assert moudle._is_top, "reset_inputs only support the top-level graph"

        inputs, tree_def = tree_flatten(((moudle, *args), kwargs))

        def create_node(val: Tensor):
            node = Input(type=TensorNode).outputs[0]
            node.shape = val.shape
            node.dtype = val.dtype
            return node

        formal_node_inputs = [
            forma_mnode,
        ]

        org_argdef = list(moudle.argdef_graph_map.keys())[0]
        if call_nodes:
            org_argdef = call_nodes[0][0].arg_def

        for v in inputs[1:]:
            assert isinstance(v, RawTensor)
            formal_node_inputs.append(create_node(v))

        actual_nodes = []
        for e, n in call_nodes:
            e.arg_def = tree_def
            actual_node_inputs = [
                n,
            ]
            for v in inputs[1:]:
                actual_node_inputs.append(create_node(v))

            for org_n in e.inputs:
                org_n.users.pop(e)

            e.inputs[:] = actual_node_inputs
            e.const_val = []
            actual_nodes.append(actual_node_inputs[1:])

        self._inputs[:] = formal_node_inputs
        moudle.argdef_graph_map[tree_def] = moudle.argdef_graph_map.pop(org_argdef)
        moudle.argdef_outdef_map[tree_def] = moudle.argdef_outdef_map.pop(org_argdef)

        # return formal_node_inputs[1:], actual_nodes
        return formal_node_inputs[1:]

    def add_input_node(self, shape, dtype="float32", name="args"):
        forma_mnode = self.inputs[0]
        actual_mnodes = forma_mnode.actual_mnode

        moudle = forma_mnode.owner
        assert moudle._is_top, "add_input_node only support the top-level graph"

        call_nodes = []
        for n in actual_mnodes:
            for c_expr in n.users:
                if isinstance(c_expr, CallMethod) and c_expr.method == "__call__":
                    call_nodes.append(c_expr)

        def create_node(name=None, is_input: bool = True):
            if is_input:
                node = Input(type=TensorNode, name=name).outputs[0]
            else:
                node = TensorNode(expr=None, name=None)
            node.shape = shape
            node.dtype = dtype
            return node

        org_argdef = list(moudle.argdef_graph_map.keys())[0]

        if call_nodes:
            org_argdef = call_nodes[0].arg_def

        args, kwargs = org_argdef.unflatten(self._inputs)
        formal_inp_node = create_node(self._create_unique_name(name), True)
        inputs, tree_def = tree_flatten(
            ((*args, formal_inp_node), kwargs),
            is_const_leaf=lambda x: not isinstance(x, (TensorNode, ModuleNode)),
        )
        self._inputs[:] = inputs[:]

        actual_inp_nodes = []
        for e in call_nodes:
            args, kwargs = e.unflatten_args(e.inputs)
            args = args + (create_node(False),)
            inputs, tree_def = tree_flatten(
                (args, kwargs),
                is_const_leaf=lambda x: not isinstance(x, (TensorNode, ModuleNode)),
            )
            e.inputs[:] = inputs[:]
            e.arg_def = tree_def
            actual_inp_nodes.append(args[-1])

        moudle.argdef_graph_map[tree_def] = moudle.argdef_graph_map.pop(org_argdef)
        moudle.argdef_outdef_map[tree_def] = moudle.argdef_outdef_map.pop(org_argdef)

        # return formal_inp_node, actual_inp_nodes
        return formal_inp_node

    def reset_outputs(self, outputs):
        outputs, out_def = tree_flatten(
            outputs, is_leaf=lambda x: isinstance(x, TensorNode),
        )
        forma_mnode = self.inputs[0]

        moudle = forma_mnode.owner
        assert moudle._is_top, "reset_outputs only support the top-level graph"

        actual_mnodes = forma_mnode.actual_mnode
        call_nodes = []
        for n in actual_mnodes:
            for c_expr in n.users:
                if isinstance(c_expr, CallMethod) and c_expr.method == "__call__":
                    call_nodes.append((c_expr))

        def create_node(val: TensorNode, expr: Expr):
            node = TensorNode(expr)
            node.shape = val.shape
            node.dtype = val.dtype
            return node

        tree_def = list(moudle.argdef_graph_map.keys())[0]
        if call_nodes:
            tree_def = call_nodes[0].arg_def

        actual_nodes = []
        for e in call_nodes:
            actual_node_outputs = []
            for v in outputs:
                actual_node_outputs.append(create_node(v, e))
            e.outputs[:] = actual_node_outputs
            e.out_def = out_def
            actual_nodes.append(actual_node_outputs)

        self._outputs[:] = outputs
        moudle.argdef_outdef_map[tree_def] = out_def

        return actual_nodes

    def add_output_node(self, node: TensorNode):
        forma_mnode = self.inputs[0]

        moudle = forma_mnode.owner
        assert moudle._is_top, "add_output_node only support the top-level graph"

        actual_mnodes = forma_mnode.actual_mnode
        call_nodes = []

        for n in actual_mnodes:
            for c_expr in n.users:
                if isinstance(c_expr, CallMethod) and c_expr.method == "__call__":
                    call_nodes.append((c_expr))

        def create_node(val: TensorNode, expr: Expr):
            node = TensorNode(expr)
            node.shape = val.shape
            node.dtype = val.dtype
            return node

        tree_def = list(moudle.argdef_graph_map.keys())[0]
        if call_nodes:
            tree_def = call_nodes[0].arg_def

        org_out_def = moudle.argdef_outdef_map[tree_def]
        org_outs = org_out_def.unflatten(self._outputs)
        outputs, out_def = tree_flatten(
            (org_outs, node), is_leaf=lambda x: isinstance(x, TensorNode),
        )
        self._outputs[:] = outputs

        actual_out_nodes = []
        for e in call_nodes:
            actual_node = create_node(node, e)
            org_outs = org_out_def.unflatten(e.outputs)
            outputs, out_def = tree_flatten(
                (org_outs, actual_node), is_leaf=lambda x: isinstance(x, TensorNode),
            )
            e.outputs[:] = outputs
            e.out_def = out_def
            actual_out_nodes.append(actual_node)

        moudle.argdef_outdef_map[tree_def] = out_def

        return actual_out_nodes

    def insert_function(self, func: Callable, *args, **kwargs):
        assert isinstance(func, Callable)

        inp_nodes, inp_def = tree_flatten((args, kwargs))

        insert_idx = -1
        for i in inp_nodes:
            if isinstance(i, TensorNode) and i.expr in self._exprs:
                insert_idx = max(insert_idx, self._exprs.index(i.expr))

        fake_inp_val = list(
            F.zeros(shape=i.shape, dtype=i.dtype) if isinstance(i, TensorNode) else i
            for i in inp_nodes
        )

        for v, n in zip(fake_inp_val, inp_nodes):
            if isinstance(n, TensorNode):
                NodeMixin.wrap_safe(v, n)

        fake_args, fake_kwargs = inp_def.unflatten(fake_inp_val)

        insert_point = self.insert_exprs_before()
        if insert_idx != -1:
            insert_point = self.insert_exprs_after(self._exprs[insert_idx])

        with insert_point:
            rst = func(*fake_args, **fake_kwargs)

        if rst is None:
            return None

        outputs, out_def = tree_flatten(rst, is_leaf=_is_leaf)
        node_outputs = []
        for out in outputs:
            assert isinstance(out, RawTensor)
            node_outputs.append(NodeMixin.get(out, None))

        node_outputs = out_def.unflatten(node_outputs)
        return node_outputs

    def insert_exprs_after(self, expr: Optional[Expr] = None):
        if expr is not None:
            assert expr.top_graph == self, "Expr to insert after is not in graph."
        return _InsertExprs(self, expr, after=True)

    def insert_exprs_before(self, expr: Optional[Expr] = None):
        if expr is not None:
            assert expr.top_graph == self, "Expr to insert before is not in graph."
        return _InsertExprs(self, expr, after=False)

    def replace_node(self, repl_dict: Dict[Node, Node]):
        while repl_dict:
            node, repl_node = repl_dict.popitem()
            # check graph inputs and outputs
            assert node not in self.inputs, "Cannot replace inputs"
            for i, n in enumerate(self.outputs):
                if n is node:
                    self.outputs[i] = repl_node
            # update users of node and repl_node
            # update inputs of expr in node.users
            dep_exprs = self.get_dep_exprs(repl_node)
            i = 0
            while i < len(node.users):
                n = node.users[i]
                if n in dep_exprs:
                    logger.info("Find a loop: ignore this replacement once")
                    logger.info("node: %s" % node.__repr__())
                    logger.info("repl_node: %s" % repl_node.__repr__())
                    i += 1
                    continue
                repl_node.users.append(n)
                node.users.pop(i)
                idx = n.inputs.index(node)
                n.inputs[idx] = repl_node

    def compile(self):
        """
        Delete unused expr.
        """
        dep_exprs = self.get_dep_exprs(self.outputs)
        i = 0
        while i < len(self._exprs):
            expr = self._exprs[i]
            if expr in dep_exprs or expr._disable_remove:
                i += 1
                continue
            for n in expr.inputs:
                n.users.remove(expr)
            self._exprs.remove(expr)

    def interpret(self, *inputs):
        node2value = {}
        end_nodes_set = set(self._end_point)
        endnode2value = {}

        def get_all_endnode_val(n, v):
            if n in end_nodes_set:
                endnode2value[n] = v
                end_nodes_set.remove(n)
                return not end_nodes_set
            return False

        for n, v in zip(self._inputs, inputs):
            node2value[n] = v
            if n in self._watch_point:
                self._rst[n].append(v)
            if n in self._end_point and get_all_endnode_val(n, v):
                return list(endnode2value[i] for i in self._end_point)

        for expr in self._exprs:
            values = expr.interpret(*list(node2value[i] for i in expr.inputs))
            if values is not None:
                for n, v in zip(expr.outputs, values):
                    node2value[n] = v
                    if n in self._watch_point:
                        self._rst[n] = v
                    if self._end_point and get_all_endnode_val(n, v):
                        return list(endnode2value[i] for i in self._end_point)

        return list(node2value[i] for i in self._outputs)

    def eval(self, *inputs):
        assert len(inputs) == len(self._inputs) - 1
        inp = [self._inputs[0].owner] + list(inputs)
        return self.interpret(*inp)

    def __repr__(self):
        return self.__format__()

    def __format__(self, format_spec: str = "") -> str:
        saved_format_spec = Node.set_format_spec(format_spec)
        name = ""
        if self._name:
            name = "%s.Graph" % self._name
        res = "{} ({}) {{\n\t{}\n\treturn {}\n}}".format(
            name,
            ", ".join(str(i) for i in self._inputs),
            "\n\t".join("{}".format(str(i)) for i in self._exprs),
            ", ".join(str(i) for i in self._outputs),
        )
        Node.set_format_spec(saved_format_spec)
        return res


def _get_meth_name(obj, func):
    tp = obj if isinstance(obj, type) else type(obj)
    for cls in tp.mro():
        for k, v in cls.__dict__.items():
            if v == func:
                return k
    return None


def _wrapped_function(orig_func):
    @functools.wraps(orig_func)
    def wrapped_fn(*args, **kwargs):
        if is_tracing_module():
            unset_module_tracing()
            inputs, tree_def = tree_flatten((args, kwargs))
            for i in inputs:
                if not NodeMixin.get(i, None):
                    if isinstance(i, (RawTensor, NodeMixin)):
                        NodeMixin.wrap_safe(i, Constant.make(i))
            meth_name = _get_meth_name(args[0], wrapped_fn) if args else None
            if meth_name:
                self = inputs[0]
                if meth_name == "__new__":
                    if all([not isinstance(i, RawTensor) for i in inputs]):
                        # only trace Tensor.__new__() when there are tensors in args
                        set_module_tracing()
                        return orig_func(*args, **kwargs)
                    if isinstance(args[1], RawTensor):
                        node = NodeMixin.get(inputs[1])
                        inputs[1] = copy.copy(inputs[1])
                        # copy inputs[1] to avoid tensor and Tensor(tensor) share same m_tensor, which will cause they have same _NodeMixin__node in tracing.
                        NodeMixin.wrap_safe(inputs[1], node)
                        args, kwargs = tree_def.unflatten(inputs)
                    call_node = CallMethod.make(self, meth_name)
                else:
                    call_node = CallMethod.make(NodeMixin.get(self), meth_name)
                call_node.add_inputs(inputs[1:])
            else:
                call_node = CallFunction.make(orig_func)
                call_node.add_inputs(inputs)

            call_node.arg_def = tree_def
            rst = orig_func(*args, **kwargs)
            if meth_name == "__setitem__":
                rst = self
            if rst is not None:
                outputs, out_def = tree_flatten(rst, is_leaf=_is_leaf)
                call_node.out_def = out_def
            else:
                outputs = None
            call_node.add_outputs(outputs)
            set_module_tracing()
            return rst
        return orig_func(*args, **kwargs)

    return wrapped_fn


class TracedModuleBuilder(NodeMixin):

    _mod = None  # type: Module
    _body = None  # type: InternalGraph
    _is_builtin = None  # type: bool
    _argdef_graph_map = None  # type: Dict[Treedef, "InternalGraph"]
    _argdef_outdef_map = None  # type: Dict[Treedef, Treedef]
    nodes = None

    __builder_attributes__ = [
        "_mod",
        "_body",
        "_NodeMixin__node",
        "_is_builtin",
        "build",
        "_record_wrapped_nodes",
        "_argdef_graph_map",
        "_argdef_outdef_map",
        "nodes",
        "__class__",
        "__dict__",
    ]

    def __init__(self, mod, is_top_module=False):
        super(TracedModuleBuilder, self).__init__()
        assert isinstance(mod, Module)
        self._mod = mod
        self._body = None
        self._is_top = is_top_module
        self._is_builtin = module_tracer.is_builtin(mod)
        self._argdef_graph_map = {}
        self._argdef_outdef_map = {}

        self.nodes = set()
        # The builder will be passed to self._mod.forward as 'self' argument. If the 'forward' uses super().xxx to call method of its base classes, the trace procedure will throw exceprion, because the builder doesn't inherit from self._mod.__bases__.
        # modify self.__class__ and let the builder inherit from TracedModuleBuilder and mod.__class__.
        self.__class__ = type(
            "TracedModuleBuilder",
            (TracedModuleBuilder, mod.__class__),
            dict(TracedModuleBuilder.__dict__),
        )

    def build(self):
        if self._is_builtin or isinstance(self._mod, TracedModule):
            for node in self.nodes:
                node.module_type = type(self._mod)
                # node._owner = weakref.ref(self._mod)
            return self._mod
        else:
            traced_module = TracedModule(
                self._is_top, self._argdef_graph_map, self._argdef_outdef_map
            )
            for _, g in self._argdef_graph_map.items():
                g.compile()

            for k, v in self.__dict__.items():
                if k not in TracedModuleBuilder.__builder_attributes__:
                    if isinstance(v, TracedModuleBuilder):
                        v = v.build()
                    setattr(traced_module, k, v)

            return traced_module

    def _record_wrapped_nodes(self, node):
        self.nodes.add(node)

    def __call__(self, *args, **kwargs):
        assert isinstance(self._mod, Module)
        # prepare args and kwargs for inner graph
        def mark_constant(x):
            node = NodeMixin.get(x, None)
            if node is None:  # capture as constant
                NodeMixin.wrap(x, lambda: Constant.make(x))

        inputs, tree_def = tree_flatten(((self, *args), kwargs))
        for i in inputs:
            mark_constant(i)
        callnode = CallMethod.make(NodeMixin.get(self))

        callnode.add_inputs(inputs[1:])

        callnode.arg_def = tree_def

        if (
            self._is_builtin
            or tree_def in self._argdef_graph_map
            or isinstance(self._mod, TracedModule)
        ):
            unset_module_tracing()
            rst = self._mod(*args, **kwargs)
            outputs, out_def = tree_flatten(rst, is_leaf=_is_leaf)
            set_module_tracing()
            if self._is_builtin:
                self._body = None
            elif tree_def in self._argdef_graph_map:
                self._body = self._argdef_graph_map[tree_def]
            else:
                self._mod._is_top = False
                self._body = self._mod.graph
                name = NodeMixin.get(self)._name
                if name:
                    self._body._name = name
        else:
            self_node = None
            orig_self = NodeMixin.get(self)
            top_graph = active_module_tracer().current_scope()
            graph_prefix_name = top_graph._name
            if top_graph._prefix_name:
                graph_prefix_name = "{}_{}".format(
                    top_graph._prefix_name, graph_prefix_name.lstrip("_")
                )
            self._body = InternalGraph(orig_self._name, prefix_name=graph_prefix_name)
            active_module_tracer().push_scope(self._body)
            # rebind self to new input node

            if self_node:
                NodeMixin.wrap_safe(self, self_node)
                active_module_tracer().current_scope().add_input(self_node)
            else:
                NodeMixin.wrap_safe(
                    self,
                    self_node
                    if self_node
                    else Input.make("self", NodeMixin.get_wrapped_type(self)),
                )
            origin_inp_node = [NodeMixin.get(i, None) for i in inputs[1:]]
            # prepare args and kwargs for inner graph
            index_args, index_kwargs = tree_def.unflatten(
                [
                    ArgsIndex(0),
                    *list(ArgsIndex(i + 1) for i in range(len(origin_inp_node))),
                ]
            )
            key2idx = getcallargs(type(self._mod).forward, *index_args, **index_kwargs)
            idx2key = {}
            for k, v in key2idx.items():
                if isinstance(v, ArgsIndex):
                    idx2key[v.index] = k
                else:
                    flatten_argidx, _ = tree_flatten(v)
                    for _i, v in enumerate(flatten_argidx):
                        if isinstance(v, ArgsIndex):
                            idx2key[v.index] = k + "_%d" % _i

            def wrap(x, name):
                if isinstance(x, (RawTensor, NodeMixin)):
                    NodeMixin.wrap(
                        x,
                        lambda: Input.make(
                            type=NodeMixin.get_wrapped_type(x), name=name
                        ),
                    )
                return x

            args = [self]
            for i, v in enumerate(inputs[1:]):
                args.append(wrap(v, idx2key[i + 1]))

            args, kwargs = tree_def.unflatten(args)
            active_module_tracer().patcher.auto_patch(
                getattr(getattr(self._mod, "forward", self._mod), "__globals__", {})
            )
            rst = type(self._mod).forward(*args, **kwargs)
            outputs, out_def = tree_flatten(rst, is_leaf=_is_leaf)
            for i in (
                outputs if isinstance(outputs, collections.abc.Sequence) else (outputs,)
            ):
                active_module_tracer().current_scope().add_output(NodeMixin.get(i))
            NodeMixin.get(self, None).actual_mnode.append(orig_self)
            NodeMixin.wrap_safe(self, orig_self)
            for arg, node in zip(inputs[1:], origin_inp_node):
                if node:
                    NodeMixin.wrap_safe(arg, node)
            active_module_tracer().pop_scope()

        # rebind output to outer graph
        callnode.out_def = out_def
        callnode.add_outputs(outputs)
        self._argdef_graph_map[callnode.arg_def] = self._body
        self._argdef_outdef_map[callnode.arg_def] = out_def
        return rst

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)

    def __repr__(self):
        return repr(self._mod)

    def __getattr__(self, name):
        if name not in self._mod.__dict__:
            attr = getattr(type(self._mod), name).__get__(self, type(self))
        else:
            attr = getattr(self._mod, name)
            if isinstance(attr, Module):
                attr = TracedModuleBuilder(attr)
            setattr(self, name, attr)
            NodeMixin.wrap(
                attr,
                lambda: GetAttr.make(
                    NodeMixin.get(self), name, type=NodeMixin.get_wrapped_type(attr)
                ),
            )
        return attr

    def __getattribute__(self, name):
        if name in TracedModuleBuilder.__builder_attributes__:
            return object.__getattribute__(self, name)
        else:
            wrapped = object.__getattribute__(self, name)
            if name in self._mod.__dict__:
                mod_attr = getattr(self._mod, name)

                if not isinstance(mod_attr, Module) and wrapped is not mod_attr:
                    wrapped = mod_attr
                    setattr(self, name, wrapped)

                if isinstance(mod_attr, Module):
                    assert mod_attr is wrapped._mod
                else:
                    assert mod_attr is wrapped

                # assert not self._is_builtin
                if isinstance(wrapped, (NodeMixin, RawTensor)):
                    NodeMixin.wrap(
                        wrapped,
                        lambda: GetAttr.make(
                            NodeMixin.get(self),
                            name,
                            type=NodeMixin.get_wrapped_type(wrapped),
                        ),
                    )

            return wrapped


class _expr_iter:
    def __init__(self, graph: InternalGraph):
        self.graph = graph

    def __iter__(self):
        for expr in self.graph._exprs:
            if isinstance(expr, CallMethod) and isinstance(expr.inputs[0], ModuleNode):
                yield expr
                if expr.graph is not None:
                    yield from expr.graph.expr_filter
            else:
                yield expr


class _node_iter:
    def __init__(self, graph: InternalGraph) -> None:
        nodes = []
        node_ids = set()
        for expr in graph.expr_filter:
            for n in expr.inputs + expr.outputs:
                if n._id in node_ids:
                    continue
                nodes.append(n)
                node_ids.add(n._id)
        self.nodes = list(sorted(nodes, key=lambda x: x._id))

    def __iter__(self):
        for node in self.nodes:
            yield node


class BaseFilter:
    def __init__(self, expr_iter: Iterable):
        self._iter = expr_iter

    def __iter__(self):
        return iter(self._iter)

    def as_list(self):
        return list(self)

    def as_dict(self):
        return collections.OrderedDict((i._id, i) for i in self)

    def as_unique(self):
        rst = self.as_list()
        assert len(rst) == 1, "{} elements found".format(len(rst))
        (expr,) = self
        return expr

    def as_count(self):
        return sum(1 for _ in self)


class ExprFilter(BaseFilter):
    def call_function(self, func):
        return ExprFilterCallFunction(self, func)

    def call_method(self, method):
        return ExprFilterCallMethod(self, method)

    def expr_id(self, expr_id: List[int]):
        return ExprFilterExprId(self, expr_id)


class NodeFilter(BaseFilter):
    def type(self, owner_type, node_type):
        return NodeFilterType(self, owner_type, node_type)

    def node_id(self, node_id: List[int]):
        return NodeFilterNodeId(self, node_id)

    def name(self, name: str, ignorecase: bool = True):
        return NodeFilterName(self, name, ignorecase)


class NodeFilterType(NodeFilter):
    def __init__(self, expr_iter, owner_type, node_type):
        super().__init__(expr_iter)
        self.owner_type = owner_type
        self.node_type = node_type

    def __iter__(self):
        for node in self._iter:
            if not isinstance(node, self.node_type):
                continue
            if not hasattr(node, "owner"):
                continue
            if isinstance(node.owner, self.owner_type):
                yield node


class NodeFilterNodeId(NodeFilter):
    def __init__(self, expr_iter, node_id: List[int]):
        super().__init__(expr_iter)
        if not isinstance(node_id, Sequence):
            node_id = [node_id]
        self.node_id = node_id

    def __iter__(self):
        for node in self._iter:
            if node._id in self.node_id:
                yield node


class NodeFilterName(NodeFilter):
    _re = None

    def __init__(self, node_iter, pattern, ignorecase):
        super().__init__(node_iter)
        self.pattern = pattern
        self._re = self.make_re(pattern, ignorecase)

    @classmethod
    def make_re(cls, pattern, ignorecase=True):
        assert isinstance(pattern, str), "bad pattern: {!r}".format(pattern)
        assert isinstance(ignorecase, bool)
        flags = 0
        if ignorecase:
            flags |= re.IGNORECASE
        return re.compile(fnmatch.translate(pattern), flags=flags)

    def __iter__(self):
        for i in self._iter:
            graph = i.top_graph
            name = "{}_{}".format(graph._name, i._name.lstrip("_"))
            if graph._prefix_name:
                name = "{}_{}".format(graph._prefix_name, name.lstrip("_"))
            if self.pattern == name or self._re.match(name):
                yield i


class ExprFilterCallFunction(ExprFilter):
    def __init__(self, expr_iter, func: Callable = None):
        super().__init__(expr_iter)
        self.func = func

    def __iter__(self):
        for expr in self._iter:
            if not isinstance(expr, CallFunction):
                continue
            if self.func is None or expr.func == self.func:
                yield expr


class ExprFilterCallMethod(ExprFilter):
    def __init__(self, expr_iter, method: str = None):
        super().__init__(expr_iter)
        self.method = method

    def __iter__(self):
        for expr in self._iter:
            if not isinstance(expr, CallMethod):
                continue
            if self.method is None or expr.method == self.method:
                yield expr


class ExprFilterExprId(ExprFilter):
    def __init__(self, expr_iter, expr_id: List[int]):
        super().__init__(expr_iter)
        if not isinstance(expr_id, Sequence):
            expr_id = [expr_id]
        self.expr_id = expr_id

    def __iter__(self):
        for expr in self._iter:
            if expr._id in self.expr_id:
                yield expr


class TracedModule(Module):
    """
    `TracedModule` is the Module created by tracing normal module. It owns an argdef to graph(InternalGraph) map. The forward method of `TracedModule` will get a graph from `argdef_graph_map` according to the argdef of input args/kwargs and interpret it.
    """

    # m_node = None  # type: ModuleNode
    argdef_graph_map = None
    argdef_outdef_map = None

    def __init__(self, is_top, argdef_graph_map, argdef_outdef_map):
        super(TracedModule, self).__init__()
        self.argdef_graph_map = argdef_graph_map
        self.argdef_outdef_map = argdef_outdef_map
        self._is_top = is_top
        self.watch_points = []
        self.watch_node_value = {}
        self.end_points = []

    def forward(self, *args, **kwargs):
        inputs, treedef = tree_flatten(((self, *args), kwargs))
        assert treedef in self.argdef_graph_map
        inputs = filter(
            lambda i: isinstance(i, (Module, TracedModuleBuilder, RawTensor)), inputs
        )  # allow TracedModuleBuilder for retrace.
        outputs = self.argdef_graph_map[treedef].interpret(*inputs)
        if self.watch_points:
            self.watch_node_value = {}
            for n in self.watch_points:
                self.watch_node_value[n] = n.top_graph._rst.pop(n)

        if self.end_points:
            return outputs

        out_def = self.argdef_outdef_map[treedef]
        outputs = out_def.unflatten(outputs)

        return outputs

    def set_watch_points(self, nodes):
        if not isinstance(nodes, Sequence):
            nodes = [nodes]
        self.watch_points = nodes
        for n in nodes:
            n.top_graph._watch_point.append(n)

    def clear_watch_points(self):
        for n in self.watch_points:
            n.top_graph._watch_point = []
        self.watch_points = []
        self.watch_node_value = {}

    def set_end_points(self, nodes):
        if not isinstance(nodes, Sequence):
            nodes = [nodes]
        self.end_points = nodes
        graphs = list(self.argdef_graph_map.values())
        for n in nodes:
            assert n.top_graph in graphs
            n.top_graph._end_point.append(n)

    def clear_end_points(self):
        for n in self.end_points:
            n.top_graph._end_point = []
        self.end_points = []

    @property
    def graph(self) -> InternalGraph:
        if self._is_top:
            self._update_ref()
        assert len(self.argdef_graph_map) == 1
        return list(self.argdef_graph_map.values())[0]

    def _update_ref(self, actual_node_map: Union[Dict] = None):
        for inp_def, graph in self.argdef_graph_map.items():
            for n in graph._inputs + graph.outputs:
                n._top_graph = weakref.ref(graph)
            graph._inputs[0]._owner = weakref.ref(self)
            graph._inputs[0].actual_mnode = []
            if actual_node_map is not None and inp_def in actual_node_map.keys():
                graph._inputs[0].actual_mnode = actual_node_map[inp_def]
            node2obj = {}
            next_actual_node_map = collections.defaultdict(
                lambda: collections.defaultdict(list)
            )
            node2obj[graph._inputs[0]] = self
            for expr in graph._exprs:
                for n in expr.inputs + expr.outputs:
                    n._top_graph = weakref.ref(graph)
                expr._top_graph = weakref.ref(graph)
                if isinstance(expr, GetAttr) and isinstance(
                    expr.outputs[0], ModuleNode
                ):
                    obj = getattr(node2obj[expr.inputs[0]], expr.name)
                    expr.outputs[0]._owner = weakref.ref(obj)
                    node2obj[expr.outputs[0]] = obj
                if isinstance(expr, Constant) and isinstance(
                    expr.outputs[0], ModuleNode
                ):
                    obj = expr.value
                    expr.outputs[0]._owner = weakref.ref(obj)
                    node2obj[expr.outputs[0]] = obj
                if (
                    isinstance(expr, CallMethod)
                    and expr.method == "__call__"
                    and isinstance(expr.inputs[0], ModuleNode)
                ):
                    obj = node2obj[expr.inputs[0]]
                    if expr.arg_def is not None:
                        next_actual_node_map[obj][expr.arg_def].append(expr.inputs[0])

            for obj in node2obj.values():
                if obj is self:
                    continue
                mnode_map = None
                if obj in next_actual_node_map.keys():
                    mnode_map = next_actual_node_map[obj]
                if isinstance(obj, TracedModule):
                    obj._update_ref(mnode_map)

    def flatten(self):
        """
        Get a new module, which eliminates ``GetAttr`` and has no hierarchy.

        :return: :class:`TracedModule`
        """
        new_module = copy.deepcopy(self)
        module2name = {}
        assert active_module_tracer() is None
        set_active_module_tracer(module_tracer(lambda x: x))
        active_module_tracer().push_scope(new_module.graph)
        for n, m in new_module.named_modules():
            module2name[id(m)] = n

        def _flatten_subgraph(
            graph: InternalGraph, module: Module, call=None, prefix_name=""
        ):
            if graph is not None and prefix_name and prefix_name[-1] != "_":
                prefix_name += "_"
            if graph is None:
                assert not isinstance(module, TracedModule)
                const = Constant(module, "self.%s" % module2name[id(module)])
                m_node = call.inputs[0]
                if m_node.top_graph != active_module_tracer().current_scope():
                    m_node._name = (
                        active_module_tracer()
                        .current_scope()
                        ._create_unique_name(prefix_name)
                    )
                const.outputs[0] = m_node
                const.outputs[0].expr = const
                return [const, call]
            if call is not None:
                graph = copy.deepcopy(graph)
            exprs = []
            node2obj = {}
            node2obj[graph._inputs[0]] = module
            if call:
                node2obj[call.inputs[0]] = module

            # replace inputs for submodule's exprx
            if call:
                repl_dict = dict(zip(graph._inputs, call.inputs))
                for ind, out in enumerate(graph.outputs):
                    if isinstance(out.expr, Input):
                        assert out in repl_dict
                        call_out = call.outputs[ind]
                        for expr in call.outputs[ind].users:
                            for index, inp in enumerate(expr.inputs):
                                if inp is call_out:
                                    expr.inputs[index] = repl_dict[out]

                        continue
                    repl_dict[out] = call.outputs[ind]

                graph._replace_inputs_outputs_and_add_prefixname(repl_dict, prefix_name)

            for expr in graph._exprs:
                if isinstance(expr, GetAttr):
                    # replace GetAttr with Constant
                    if isinstance(expr.outputs[0], TensorNode):
                        const = Constant(getattr(node2obj[expr.inputs[0]], expr.name))
                        const.outputs = expr.outputs
                        const.outputs[0].expr = const
                        exprs.append(const)
                    elif isinstance(expr.outputs[0], ModuleNode):
                        node2obj[expr.outputs[0]] = getattr(
                            node2obj[expr.inputs[0]], expr.name
                        )

                elif isinstance(expr, CallMethod):
                    obj_node = expr.inputs[0]
                    if isinstance(obj_node, ModuleNode):
                        pre_expr = expr.inputs[0].expr
                        if isinstance(pre_expr, GetAttr):
                            (obj,) = pre_expr.interpret(node2obj[pre_expr.inputs[0]])
                            expr_graph = (
                                obj.argdef_graph_map[expr.arg_def]
                                if hasattr(obj, "argdef_graph_map")
                                else None
                            )
                            exprs.extend(
                                _flatten_subgraph(
                                    expr_graph,
                                    obj,
                                    expr,
                                    prefix_name + obj_node._name.lstrip("_"),
                                )
                            )
                        else:
                            # module has been replaced.
                            assert isinstance(pre_expr, Constant)
                            exprs.append(expr)
                    else:
                        exprs.append(expr)
                else:
                    exprs.append(expr)

            if call is not None:
                for i in call.inputs:
                    i.users.remove(call)

            return exprs

        new_module.graph._exprs = _flatten_subgraph(new_module.graph, new_module)
        new_module.graph.compile()
        set_active_module_tracer(None)
        for _id, expr in enumerate(new_module.graph._exprs):
            expr._id = _id
        total_node_id = 0
        for i in new_module.graph._inputs:
            i._id = total_node_id
            total_node_id += 1
        for expr in new_module.graph._exprs:
            for o in expr.outputs:
                o._id = total_node_id
                total_node_id += 1
        return new_module

    def __getstate__(self):
        d = self.__dict__
        for k in Module.__dict__:
            d.pop(k, None)
        return d


def cpp_apply_module_trace(opdef, *args):
    return Apply.apply_module_trace_hook(opdef, *args)


def register_as_builtin(mod_cls: Type[Module]) -> None:
    """
    Registers class ``mod_cls`` (subclass of megengine.module.Module) as builtin module.

    param mod_cls: the Module class which will be threated as builtin module in tracing
    """
    module_tracer.register_as_builtin(mod_cls)


wrap = _wrapped_function


def _register_all_builtin_module():

    for sub_mod in [M, M.qat, M.quantized]:
        for m in getmembers(sub_mod):
            if (
                isclass(m[1])
                and issubclass(m[1], M.Module)
                and m[1] is not M.Sequential
            ):
                module_tracer.register_as_builtin(m[1])


def trace_module(mod: Module, *args: Tensor, **kwargs: Tensor) -> TracedModule:
    """
    Traces module ``mod`` and returns corresponding TracedModule.

    param mod: the module will be converted to TracedModule
    param input: the positional arguments passed to forward method of ``mod``
    param kwargs: the keyword arguments passed to forward method of ``mod``
    """
    assert active_module_tracer() is None
    assert isinstance(mod, Module)
    try:
        use_sym_shape = set_symbolic_shape(True)
        set_module_tracing()
        set_active_module_tracer(module_tracer(_wrapped_function))

        with active_module_tracer().patcher:
            global_scope = InternalGraph(name="")
            active_module_tracer().push_scope(global_scope)
            builder = TracedModuleBuilder(mod, True)
            name = mod._name if mod._name else mod.__class__.__name__
            NodeMixin.wrap_safe(builder, Input.make(name, ModuleNode))
            inputs, _ = tree_flatten((args, kwargs))
            for _, i in enumerate(inputs):
                # assert isinstance(i, Tensor), "not support "
                if isinstance(i, RawTensor):
                    NodeMixin.wrap_safe(
                        i, Input.make("arg_{}".format(_), NodeMixin.get_wrapped_type(i))
                    )
            builder(*args, **kwargs)
            active_module_tracer().pop_scope()
            return builder.build()
    finally:
        set_symbolic_shape(use_sym_shape)
        set_active_module_tracer(None)
        unset_module_tracing()
