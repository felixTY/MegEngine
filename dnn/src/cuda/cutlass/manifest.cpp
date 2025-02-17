/***************************************************************************************************
 * Copyright (c) 2017-2020, NVIDIA CORPORATION.  All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 *modification, are permitted provided that the following conditions are met:
 *     * Redistributions of source code must retain the above copyright notice,
 *this list of conditions and the following disclaimer.
 *     * Redistributions in binary form must reproduce the above copyright
 *notice, this list of conditions and the following disclaimer in the
 *documentation and/or other materials provided with the distribution.
 *     * Neither the name of the NVIDIA CORPORATION nor the names of its
 *contributors may be used to endorse or promote products derived from this
 *software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 *AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 *IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 *DISCLAIMED. IN NO EVENT SHALL NVIDIA CORPORATION BE LIABLE FOR ANY DIRECT,
 *INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 * BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 *DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
 *OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TOR (INCLUDING
 *NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
 *EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 **************************************************************************************************/
/**
 * \file dnn/src/cuda/cutlass/manifest.cpp
 * MegEngine is Licensed under the Apache License, Version 2.0 (the "License")
 *
 * Copyright (c) 2014-2021 Megvii Inc. All rights reserved.
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT ARRANTIES OR CONDITIONS OF ANY KIND, either express or
 * implied.
 */

#include <memory>

#include "src/cuda/cutlass/manifest.h"

namespace cutlass {
namespace library {

//////////////////////////////////////////////////////////////////////////////////////////////////////////

/// Top-level initialization
Status Manifest::initialize() {
    if (!operations_.empty()) {
        operations_.clear();
    }

    // initialize procedurally generated cutlass op in manifest object
    initialize_all(*this);

    return Status::kSuccess;
}

/// Used for initialization
void Manifest::reserve(size_t operation_count) {
    operations_.reserve(operation_count);
}

/// Graceful shutdown
Status Manifest::release() {
    operations_.clear();
    return Status::kSuccess;
}

/// Appends an operation and takes ownership
void Manifest::append(Operation* operation_ptr) {
    operations_.emplace_back(operation_ptr);
}

/// Returns an iterator to the first operation
OperationVector const& Manifest::operations() const {
    return operations_;
}

/// Returns a const iterator
OperationVector::const_iterator Manifest::begin() const {
    return operations_.begin();
}

/// Returns a const iterator
OperationVector::const_iterator Manifest::end() const {
    return operations_.end();
}

///////////////////////////////////////////////////////////////////////////////////////////////////

}  // namespace library
}  // namespace cutlass

///////////////////////////////////////////////////////////////////////////////////////////////////
