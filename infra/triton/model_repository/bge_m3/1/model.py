"""Triton Python-backend model for BAAI/bge-m3 dense embeddings.

Loads the sentence-transformers ``BAAI/bge-m3`` checkpoint at
container start (first download takes a minute on cold cache) and
serves dense, L2-normalized vectors over Triton's V2 inference API.

Contract (mirrors `app/services/rag/triton_client.py` on the client
side):

  Input  "TEXT": BYTES, shape ``[N]`` — N raw UTF-8 texts.
  Output "embedding": FP32, shape ``[N, 1024]``.

The model file lives at:
    /models/bge_m3/1/model.py

Triton's Python backend imports ``TritonPythonModel`` and calls
``initialize`` once, then ``execute`` per request. Heavy work (model
load) happens in ``initialize`` so the first inference is fast.
"""
from __future__ import annotations

import os

import numpy as np
import triton_python_backend_utils as pb_utils


_MODEL_NAME = os.environ.get("BGE_MODEL", "BAAI/bge-m3")
# Sentence-transformers picks "cuda" automatically when available;
# override to "cpu" via TRITON_FORCE_CPU=1 for laptop demos.
_DEVICE = "cpu" if os.environ.get("TRITON_FORCE_CPU", "1") == "1" else None
_MAX_LEN = int(os.environ.get("BGE_MAX_LEN", "512"))


class TritonPythonModel:
    """Triton entrypoint — see module docstring for the contract."""

    def initialize(self, args):
        # Defer the heavy import until container start; the Triton
        # bootstrapper doesn't want top-level torch imports racing.
        from sentence_transformers import SentenceTransformer

        kwargs: dict = {}
        if _DEVICE is not None:
            kwargs["device"] = _DEVICE
        self._model = SentenceTransformer(_MODEL_NAME, **kwargs)
        # bge-m3 supports up to 8192 but 512 is plenty for the schema
        # chunks + question embeddings the agent sends; keeps CPU
        # latency under ~200 ms/query on Ryzen-class hardware.
        self._model.max_seq_length = _MAX_LEN
        # Sanity log so the user can confirm the model loaded on cold
        # start by tailing the container.
        try:
            dim = self._model.get_sentence_embedding_dimension()
        except Exception:
            dim = "?"
        print(
            f"[bge_m3] loaded model={_MODEL_NAME} device={_DEVICE or 'auto'} "
            f"dim={dim} max_seq_len={_MAX_LEN}",
            flush=True,
        )

    def execute(self, requests):
        responses: list = []
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

            vectors = self._model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
                batch_size=min(32, len(texts)),
            )
            vectors = np.asarray(vectors, dtype=np.float32)
            if vectors.ndim == 1:
                # encode() returns 1-D for a single text — reshape so
                # the output is always [N, dim].
                vectors = vectors.reshape(1, -1)

            out = pb_utils.Tensor("embedding", vectors)
            responses.append(pb_utils.InferenceResponse([out]))
        return responses

    def finalize(self):
        # sentence-transformers doesn't expose an explicit close; let
        # GC handle the torch tensors when the model is torn down.
        self._model = None
