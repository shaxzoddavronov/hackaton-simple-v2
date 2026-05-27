"""Triton Python-backend model for BAAI/bge-m3 dense embeddings.

Tuned for NVIDIA L40S (Ada Lovelace, sm_89, 48 GB VRAM) but degrades
gracefully on CPU when ``TRITON_FORCE_CPU=1``.

Inference defaults on L40S:
  * Weights:        FP16 (BGE_PRECISION=fp16) — 2× throughput vs FP32
                    on Tensor Cores; negligible accuracy loss for the
                    1024-d retrieval task.
  * Batch size:     64 (BGE_BATCH_SIZE=64) — well below the ~3 GB
                    activation budget for seq=512 at batch=64.
  * Max seq len:    512 — bge-m3 supports 8192 but the agent's chunks
                    are <= 1200 chars; longer context wastes attention
                    compute.
  * Eval mode +     The model is wrapped in ``torch.inference_mode()``
    no_grad:        so autograd doesn't allocate graph tape; this alone
                    cuts ~15% latency vs the stock encode() path.

Cold-start cost on L40S: ~5 s (CUDA init + checkpoint copy from
HuggingFace cache to device). Steady-state latency for a single query:
~3-5 ms; for a 32-text batch: ~10-15 ms.

Contract (matches `app/services/rag/triton_client.py`):
  Input  "TEXT"      : BYTES, shape ``[N]`` — N raw UTF-8 texts.
  Output "embedding" : FP32, shape ``[N, 1024]``.

We always cast the output back to FP32 on the wire — pgvector stores
FP32 and the client expects FP32, so the FP16 weights/activations are
an implementation detail.
"""
from __future__ import annotations

import os
import time

import numpy as np
import triton_python_backend_utils as pb_utils


_MODEL_NAME = os.environ.get("BGE_MODEL", "BAAI/bge-m3")
# Default to GPU when available. Set TRITON_FORCE_CPU=1 on CPU-only
# hosts (or to debug a CUDA mismatch quickly).
_FORCE_CPU = os.environ.get("TRITON_FORCE_CPU", "0") == "1"
_MAX_LEN = int(os.environ.get("BGE_MAX_LEN", "512"))
_BATCH = int(os.environ.get("BGE_BATCH_SIZE", "64"))
# Precision: "fp16" (default on GPU, 2x throughput on L40S Tensor Cores),
# "bf16" (also Tensor-Core accelerated; better numerical stability than
# fp16 for some attention patterns — try this if you see retrieval-
# quality regression), or "fp32" (debug / CPU).
_PRECISION = os.environ.get("BGE_PRECISION", "fp16").lower()


class TritonPythonModel:
    """Triton entrypoint — see module docstring for the contract."""

    def initialize(self, args):
        # Heavy imports go inside initialize so the Triton bootstrapper
        # doesn't race on top-level torch imports across model loads.
        import torch
        from sentence_transformers import SentenceTransformer

        # Device + dtype dispatch.
        if _FORCE_CPU or not torch.cuda.is_available():
            self._device = "cpu"
            torch_dtype = torch.float32
            self._use_amp = False
            precision_str = "fp32"
        else:
            self._device = "cuda"
            self._use_amp = _PRECISION in ("fp16", "bf16")
            if _PRECISION == "bf16":
                torch_dtype = torch.bfloat16
                precision_str = "bf16"
            elif _PRECISION == "fp16":
                torch_dtype = torch.float16
                precision_str = "fp16"
            else:
                torch_dtype = torch.float32
                precision_str = "fp32"

        # Construct the model on the chosen device. We pass model_kwargs
        # so the underlying transformer weights are loaded in the target
        # dtype directly instead of upcast-then-cast (saves ~1 GB of
        # transient VRAM during load on bge-m3).
        self._model = SentenceTransformer(
            _MODEL_NAME,
            device=self._device,
            model_kwargs={"torch_dtype": torch_dtype}
            if self._device == "cuda"
            else None,
        )
        # bge-m3 caps at 8192; 512 is plenty for our chunk windows and
        # cuts the attention compute proportional to seq_len^2.
        self._model.max_seq_length = _MAX_LEN
        # Eval mode disables dropout (no-op for inference but explicit
        # is better) and shaves a few micros off forward passes.
        self._model.eval()
        self._torch = torch

        # Surface device + memory state in the log so the operator can
        # verify L40S was actually picked up. ``nvidia-smi`` is the
        # ground truth, but printing it here saves a kubectl exec.
        try:
            dim = self._model.get_sentence_embedding_dimension()
        except Exception:
            dim = "?"
        if self._device == "cuda":
            props = torch.cuda.get_device_properties(0)
            mem_gb = props.total_memory / (1024**3)
            cc = f"sm_{props.major}{props.minor}"
            extra = (
                f"device=cuda name={props.name!r} cc={cc} "
                f"vram={mem_gb:.1f}GB precision={precision_str}"
            )
        else:
            extra = f"device=cpu precision={precision_str}"
        print(
            f"[bge_m3] loaded model={_MODEL_NAME} dim={dim} "
            f"max_seq_len={_MAX_LEN} batch={_BATCH} {extra}",
            flush=True,
        )

    def execute(self, requests):
        responses: list = []
        torch = self._torch
        for request in requests:
            text_tensor = pb_utils.get_input_tensor_by_name(request, "TEXT")
            raw = text_tensor.as_numpy().reshape(-1)
            texts = [
                t.decode("utf-8") if isinstance(t, (bytes, bytearray)) else str(t)
                for t in raw.tolist()
            ]

            if not texts:
                # Empty batch — return a 0-row FP32 array so the client
                # sees shape [0, 1024] rather than an error.
                empty = np.zeros((0, 1024), dtype=np.float32)
                out = pb_utils.Tensor("embedding", empty)
                responses.append(pb_utils.InferenceResponse([out]))
                continue

            t0 = time.perf_counter()
            with torch.inference_mode():
                # ``encode`` already chunks internally by batch_size, but
                # we tighten the batch to BGE_BATCH_SIZE so VRAM usage is
                # predictable under bursty index jobs.
                vectors = self._model.encode(
                    texts,
                    normalize_embeddings=True,
                    convert_to_numpy=False,  # keep on-device → cast once
                    show_progress_bar=False,
                    batch_size=min(_BATCH, len(texts)),
                )

            # ``encode`` returns either a tensor (when convert_to_numpy
            # is False) or a stacked CPU array. Normalise to a CPU FP32
            # numpy array regardless of where it landed.
            if hasattr(vectors, "device"):
                # Torch tensor path — cast to FP32 on device, then copy.
                vectors = vectors.to(dtype=torch.float32).cpu().numpy()
            else:
                vectors = np.asarray(vectors, dtype=np.float32)
            if vectors.ndim == 1:
                vectors = vectors.reshape(1, -1)

            out = pb_utils.Tensor("embedding", vectors)
            responses.append(pb_utils.InferenceResponse([out]))

            # Throughput print on long batches helps tune RAG_INDEX_BATCH.
            n = len(texts)
            if n >= 16:
                dt = (time.perf_counter() - t0) * 1000
                print(
                    f"[bge_m3] encoded n={n} in {dt:.1f}ms "
                    f"({n / max(dt, 0.001) * 1000:.0f} texts/s)",
                    flush=True,
                )
        return responses

    def finalize(self):
        # sentence-transformers doesn't expose an explicit close. We
        # drop the reference and ask CUDA to release its caching
        # allocator so the next Triton model load on the same device
        # starts with a clean slate (matters when Triton hot-reloads
        # the model directory).
        self._model = None
        try:
            self._torch.cuda.empty_cache()
        except Exception:
            pass
