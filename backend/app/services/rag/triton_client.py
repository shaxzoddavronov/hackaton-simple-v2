"""Async HTTP client for Triton Inference Server (embeddings only).

Talks to Triton's V2 inference API:
    POST {TRITON_URL}/v2/models/{model}[/versions/{ver}]/infer

The request body is the standard KServe v2 JSON format:

    {
      "inputs": [{"name": "TEXT", "shape": [N], "datatype": "BYTES",
                  "data": ["string1", "string2", ...]}]
    }

The response is mirrored as ``outputs[0].data`` containing the
``N * embedding_dim`` floats. We reshape to ``(N, dim)`` and L2-normalize
so cosine similarity reduces to a dot product.

The exact input/output tensor names depend on how the embedding model is
packaged for Triton. We allow them to be overridden via env (TRITON_INPUT_NAME,
TRITON_OUTPUT_NAME) if a different bge-m3 package is used.

This module **never** talks to vLLM and **never** parses LLM output; it is
the embedding-only counterpart to :mod:`app.agents.llm`.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from typing import Sequence

import httpx

from app.config import settings

log = logging.getLogger(__name__)


class TritonError(RuntimeError):
    """Raised when Triton returns a non-200 response or the body is malformed."""


class TritonUnavailable(TritonError):
    """Raised on transport errors (connection refused, DNS, timeout)."""


@dataclass(slots=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    model: str
    dim: int


# Triton tensor names — overridable so the packaged model can be swapped
# without touching code. Defaults match the common bge-m3 ONNX export.
_INPUT_NAME = os.environ.get("TRITON_INPUT_NAME", "TEXT")
_OUTPUT_NAME = os.environ.get("TRITON_OUTPUT_NAME", "embedding")


def _normalize(vec: Sequence[float]) -> list[float]:
    """L2-normalize so that cosine == dot. Idempotent on already-normed input."""
    s = math.sqrt(sum(x * x for x in vec))
    if s == 0:
        return list(vec)
    return [x / s for x in vec]


class TritonEmbeddingClient:
    """Thin async wrapper around Triton's REST v2 inference endpoint.

    Single shared instance per process via :func:`get_client`. The underlying
    ``httpx.AsyncClient`` is created lazily and reused.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        version: str = "",
        timeout_s: float = 30.0,
        dim: int = 1024,
        api_key: str = "",
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._version = version
        self._timeout_s = timeout_s
        self._dim = dim
        # NO httpx client cache. Celery's solo pool runs each task in its
        # own ``asyncio.run(...)`` which closes the loop on exit. Any
        # objects bound to that loop (httpx.AsyncClient's connection
        # pool, the asyncio.Lock we used to guard the cache) became
        # orphaned and silently swallowed the next task's POST as a
        # 30 s timeout with an empty error message. The fix is to
        # create a fresh httpx.AsyncClient per embed() call — the
        # per-request setup cost is negligible (~1 ms) compared to
        # the actual encode pass (~4 s for a batch of 32 on CPU).
        self._headers: dict[str, str] = {}
        if api_key:
            # ``Bearer <token>`` for NIM/ingress, ``<token>`` for ``x-api-key``-
            # style gateways. Empty scheme means "send the raw token".
            value = f"{auth_scheme} {api_key}".strip() if auth_scheme else api_key
            self._headers[auth_header] = value

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def enabled(self) -> bool:
        return bool(self._base_url)

    async def aclose(self) -> None:
        # No-op: we no longer cache an httpx client between calls.
        # Kept for API compatibility with callers that expect the
        # method to exist (the agent's federated executor uses the
        # same pattern across engines).
        return None

    def _url(self) -> str:
        if self._version:
            return f"{self._base_url}/v2/models/{self._model}/versions/{self._version}/infer"
        return f"{self._base_url}/v2/models/{self._model}/infer"

    async def embed(self, texts: Sequence[str]) -> EmbeddingResponse:
        """Embed a batch of texts. Returns L2-normalized vectors.

        Empty input returns an empty response without making a network call.
        Raises :class:`TritonUnavailable` on transport errors,
        :class:`TritonError` on protocol-level issues.
        """
        if not self.enabled:
            raise TritonUnavailable("TRITON_URL is empty — RAG embedding disabled")
        if not texts:
            return EmbeddingResponse(vectors=[], model=self._model, dim=self._dim)

        payload = {
            "inputs": [
                {
                    "name": _INPUT_NAME,
                    "shape": [len(texts)],
                    "datatype": "BYTES",
                    "data": list(texts),
                }
            ]
        }
        # Fresh httpx client per call — see __init__ for why. The
        # async with block guarantees the connection is closed before
        # the surrounding asyncio loop exits, so Celery's solo pool
        # never sees a half-open socket carrying over to the next
        # task.
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                headers=self._headers or None,
            ) as client:
                resp = await client.post(self._url(), json=payload)
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
            raise TritonUnavailable(
                f"Triton unreachable: {type(e).__name__}: {e or '(no message)'}"
            ) from e

        if resp.status_code != 200:
            raise TritonError(
                f"Triton returned {resp.status_code}: {resp.text[:500]}"
            )

        try:
            body = resp.json()
        except Exception as e:
            raise TritonError(f"Triton response is not JSON: {e}") from e

        outputs = body.get("outputs") or []
        target = None
        for o in outputs:
            if o.get("name") == _OUTPUT_NAME:
                target = o
                break
        if target is None and outputs:
            # Some packagings rename the output; accept the single output if
            # there's no ambiguity.
            target = outputs[0]
        if target is None:
            raise TritonError("Triton response missing 'outputs' tensor")

        flat = target.get("data") or []
        shape = target.get("shape") or [len(texts), self._dim]
        if len(shape) != 2:
            raise TritonError(f"Unexpected embedding shape {shape}")
        n, dim = shape
        if dim != self._dim:
            log.warning(
                "Triton embedding dim mismatch: got %d, configured %d", dim, self._dim
            )
        if len(flat) != n * dim:
            raise TritonError(
                f"Embedding payload size {len(flat)} != n*dim {n*dim}"
            )

        vectors = [
            _normalize(flat[i * dim : (i + 1) * dim]) for i in range(n)
        ]
        return EmbeddingResponse(vectors=vectors, model=self._model, dim=dim)


_client: TritonEmbeddingClient | None = None


def get_client() -> TritonEmbeddingClient:
    """Process-wide singleton."""
    global _client
    if _client is None:
        _client = TritonEmbeddingClient(
            base_url=settings.TRITON_URL,
            model=settings.TRITON_EMBED_MODEL,
            version=settings.TRITON_EMBED_MODEL_VERSION,
            timeout_s=settings.TRITON_TIMEOUT_S,
            dim=settings.EMBEDDING_DIM,
            api_key=settings.TRITON_API_KEY,
            auth_header=settings.TRITON_AUTH_HEADER,
            auth_scheme=settings.TRITON_AUTH_SCHEME,
        )
    return _client


def reset_client_for_tests() -> None:
    """Reset the singleton; only used by tests injecting a fake client."""
    global _client
    _client = None


__all__ = [
    "TritonEmbeddingClient",
    "EmbeddingResponse",
    "TritonError",
    "TritonUnavailable",
    "get_client",
    "reset_client_for_tests",
]
