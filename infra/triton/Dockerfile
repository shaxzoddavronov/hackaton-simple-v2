# Triton Inference Server image tuned for NVIDIA L40S (Ada Lovelace,
# compute capability 8.9, 48 GB VRAM).
#
# Why a GPU base by default now:
#   * L40S is a server-class GPU; running the embedder on CPU wastes
#     ~95% of the per-query latency budget. FP16 forward of bge-m3 on
#     L40S is ~3-5 ms; the CPU path is ~80-200 ms.
#   * 48 GB of VRAM has room for many instances; a single bge-m3 in
#     FP16 weighs ~1.2 GB, so we comfortably co-locate 2 model
#     instances (see config.pbtxt instance_group.count = 2) with
#     headroom for activations + KV / other workloads on the same
#     card.
#   * Ada Lovelace ships strong FP8 support, but bge-m3 is small
#     enough that FP16 is already throughput-bound by attention; we
#     stay on FP16 for portability and accuracy.
#
# Falling back to CPU: set TRITON_FORCE_CPU=1 at runtime. The image
# still works (CUDA wheels load on CPU-only hosts); only the inference
# device flag flips.

FROM nvcr.io/nvidia/tritonserver:24.06-py3

# CUDA arch targets — L40S is sm_89 (Ada Lovelace). We also keep
# sm_80 (A100 Ampere) and sm_90 (H100 Hopper) so the same image runs
# on adjacent server fleets without a rebuild. Older datacenter GPUs
# (V100 / T4) are deliberately dropped — the project's perf target is
# L40S-class.
ENV TORCH_CUDA_ARCH_LIST="8.0;8.9;9.0"

# Triton's python_backend uses /usr/bin/python3. Install our deps
# there with the CUDA 12.4 PyTorch wheel — matches the cuDNN / NCCL
# bundled in tritonserver:24.06-py3. ``torch==2.3.1`` is verified
# against ``sentence-transformers==3.0.1``.
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cu124 \
        torch==2.3.1 \
    && python3 -m pip install --no-cache-dir \
        sentence-transformers==3.0.1 \
        transformers==4.41.2 \
        huggingface-hub==0.23.4 \
        accelerate==0.31.0

# Pre-create the HF cache dir so a mounted volume has the right
# ownership.
RUN mkdir -p /root/.cache/huggingface

# A few CUDA quality-of-life env vars:
#   * PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True keeps the
#     allocator from fragmenting after long-running indexing jobs.
#   * TOKENIZERS_PARALLELISM=false silences the HuggingFace warning;
#     the tokenizer is already running in a forked Triton worker.
ENV PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    TOKENIZERS_PARALLELISM=false \
    BGE_PRECISION=fp16 \
    BGE_MAX_LEN=512 \
    BGE_BATCH_SIZE=64

# Default command. docker-compose / k8s overrides as needed.
# --log-info=1 surfaces the GPU device + memory info on startup so
# the operator can confirm L40S was actually picked up.
CMD ["tritonserver", "--model-repository=/models", \
     "--http-port=8001", \
     "--metrics-port=8002", \
     "--strict-model-config=false", \
     "--log-info=1"]
