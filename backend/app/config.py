"""Typed application settings, loaded from environment / .env file.

All runtime configuration enters the app through this module — never read
`os.environ` directly elsewhere. Fields mirror ``backend/.env.example``.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Values come from (in order of precedence): process environment, then
    the ``.env`` file at the backend root. Unknown keys are ignored so a
    shared ``.env`` can hold both backend- and infra-level variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Application metadata DB -------------------------------------------------
    DATABASE_URL: str = Field(
        default="sqlite+aiosqlite:///./querymind.db",
        description="Async SQLAlchemy URL for the QueryMind metadata DB.",
    )

    # --- Broker / cache ----------------------------------------------------------
    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    # --- Environment ------------------------------------------------------------
    # Set QM_ENVIRONMENT=production in deployments. The lifespan hook in
    # main.py fail-fasts on insecure defaults (JWT_SECRET / QM_MASTER_KEY)
    # only when this is "production". Dev / test leave the defaults alone
    # so unit tests don't need ceremony.
    QM_ENVIRONMENT: str = Field(default="dev")

    # --- Auth --------------------------------------------------------------------
    JWT_SECRET: str = Field(default="dev-insecure-change-me")
    JWT_ALG: str = Field(default="HS256")
    JWT_EXPIRES_MIN: int = Field(default=60, ge=1)

    # --- Credential-at-rest encryption ------------------------------------------
    # Url-safe base64-encoded 32-byte key. See `.env.example` for a generator.
    QM_MASTER_KEY: str = Field(default="REPLACE_WITH_BASE64_32_BYTE_KEY")

    # --- CORS -------------------------------------------------------------------
    # Comma-separated list of allowed origins for the frontend dev server.
    # Default covers both :3000 (vanilla Next.js) and :3001 (used in this
    # repo because :3000 may be taken by a local LLM UI).
    CORS_ORIGINS: str = Field(
        default="*"
    )

    # --- Local LLM (vLLM, OpenAI-compatible) ------------------------------------
    VLLM_ENDPOINT: str = Field(default="http://localhost:8000/v1")
    VLLM_MODEL: str = Field(default="google/gemma-3-4b-it")
    # Some OpenAI-compat servers (Open WebUI, LiteLLM, hosted vLLM behind
    # a gateway) require a real bearer token. Local plain vLLM does not.
    VLLM_API_KEY: str = Field(default="not-needed")

    # --- Triton (embeddings only — never LLM) -----------------------------------
    # Triton serves the embedding model over its HTTP inference API.
    # Default points at the local docker compose service. Set TRITON_URL=""
    # to disable RAG entirely (retriever falls back to BM25 pruner).
    TRITON_URL: str = Field(default="http://localhost:8001")
    TRITON_EMBED_MODEL: str = Field(default="bge_m3")
    TRITON_EMBED_MODEL_VERSION: str = Field(default="")  # empty = latest
    TRITON_TIMEOUT_S: float = Field(default=30.0, gt=0)
    # Optional bearer token. Required when Triton is fronted by a reverse
    # proxy that enforces auth (NIM, ingress, API gateway). Leave blank
    # for an unauthenticated local Triton.
    TRITON_API_KEY: str = Field(default="")
    # Header name to send the token under. NVIDIA NIM expects Bearer
    # under ``Authorization``; some gateways prefer ``x-api-key``.
    TRITON_AUTH_HEADER: str = Field(default="Authorization")
    TRITON_AUTH_SCHEME: str = Field(default="Bearer")
    EMBEDDING_DIM: int = Field(default=1024, ge=8)  # bge-m3 = 1024

    # --- RAG behaviour ----------------------------------------------------------
    RAG_TOP_K: int = Field(default=12, ge=1)
    RAG_INDEX_BATCH: int = Field(default=32, ge=1)
    # Daily diff check — Celery beat fires at this hour (UTC).
    RAG_DIFF_CHECK_HOUR_UTC: int = Field(default=0, ge=0, le=23)
    RAG_DIFF_CHECK_MINUTE_UTC: int = Field(default=0, ge=0, le=59)

    # --- Federation ------------------------------------------------------------
    # Per-connection table cap in the federated_planner prompt. Keeps the
    # token budget bounded when a workspace has many large DBs.
    FEDERATED_TOP_K: int = Field(default=6, ge=1)
    # Hard cap on the post-merge ResultSet. A bad join can multiply row
    # counts; we truncate at this many rows and set `truncated=True`.
    FEDERATION_MAX_ROWS: int = Field(default=1000, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide singleton ``Settings`` instance."""
    return Settings()


# Convenience alias so callers can do ``from app.config import settings``.
settings = get_settings()
