/**
 * \file dnn/src/cuda/convolution3d/backward_filter/chanwise.cpp
 * MegEngine is Licensed under the Apache License, Version 2.0 (the "License")
 *
 * Copyright (c) 2014-2021 Megvii Inc. All rights reserved.
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT ARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 */

#include "./algo.h"
#include "src/cuda/utils.h"
#include "src/cuda/convolution3d/chanwise/kern.cuh"

using namespace megdnn;
using namespace cuda;
using namespace convolution3d;

bool Convolution3DBackwardFilterImpl::AlgoChanwise::is_available(
        const SizeArgs &args) const {
    if (!args.src_layout->is_contiguous() ||
        !args.diff_layout->is_contiguous()) {
        return false;
    }
    auto &&fm = args.grad_filter_meta;
    return fm.format == Param::Format::NCDHW &&
        args.diff_layout->dtype.category() == DTypeCategory::FLOAT &&
        fm.spatial_ndim == 3 && fm.icpg == 1 &&
        fm.dilation[0] == 1 && fm.dilation[1] == 1 &&
        fm.dilation[2] == 1 && 
        !fm.should_flip;
}

size_t Convolution3DBackwardFilterImpl::AlgoChanwise::get_workspace_in_bytes(
        const SizeArgs &) const {
    return 0;
}

void Convolution3DBackwardFilterImpl::AlgoChanwise::exec(
        const ExecArgs &args) const {
    auto kparam = chanwise::Param::from_fwd_args(args.as_fwd_args());
    auto stream = cuda_stream(args.handle);
    switch (args.diff_layout->dtype.enumv()) {
#define cb(_dt) \
        case DTypeTrait<_dt>::enumv: \
            { \
                using ctype = DTypeTrait<_dt>::ctype; \
                return chanwise::run_bwd_filter( \
                        args.grad_tensor->ptr<ctype>(), \
                        args.src_tensor->ptr<ctype>(), \
                        args.diff_tensor->ptr<ctype>(), \
                        kparam, stream); \
            }
    MEGDNN_FOREACH_COMPUTING_DTYPE_FLOAT(cb)
#undef cb
        default:
            break;
    }
    megdnn_assert_internal(0);
}

// vim: syntax=cpp.doxygen

