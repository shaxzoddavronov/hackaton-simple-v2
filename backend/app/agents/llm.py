from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings

T = TypeVar("T", bound=BaseModel)

log = logging.getLogger(__name__)


# How many bytes of the malformed payload we feed back into the repair
# prompt. Most models repair small payloads fine; sending too much risks
# blowing the context window or echoing the same garbage back.
_REPAIR_PAYLOAD_BYTES = 4000


def _extract_first_json_object(text: str) -> str:
    """Best-effort JSON salvage from a possibly-noisy LLM completion.

    Handles these failure modes we observed in practice:
      - Trailing pad characters (especially ``\\n\\n\\n…`` floods when
        guided decoding isn't actually enforced by the server).
      - Markdown ``` ```json fences around the payload.
      - A short prose preamble followed by a ``{ … }`` object.

    The function walks the string once, tracking brace depth and string
    state, and returns the substring spanning the first balanced
    top-level ``{…}``. If no balanced object is found, returns the
    trimmed input unchanged so ``pydantic`` can fail with its native
    error.
    """
    text = text.strip()
    if not text:
        return text

    # Strip a leading ```json fence if present.
    fence = re.match(r"^```(?:json)?\s*\n?", text)
    if fence:
        text = text[fence.end() :]
        end = text.rfind("```")
        if end >= 0:
            text = text[:end]
        text = text.strip()

    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_str:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if start < 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return text[start : i + 1]
    # Unbalanced — surface the original trimmed text so the caller's
    # error message points at the real payload.
    return text


class LLMClient:
    """Thin wrapper around the OpenAI-compatible vLLM endpoint.

    Every structured call passes a Pydantic-derived JSON Schema as
    ``response_format`` so the server's guided-decoding backend
    (``xgrammar`` on vLLM) constrains decoding.

    When the server doesn't actually enforce the schema (older vLLM
    versions, LiteLLM proxies, Open WebUI), the model may emit padded
    or truncated JSON that ``pydantic`` rejects. We defend in two
    stages:

      1. **Salvage** — extract the first balanced ``{…}`` from the raw
         output, stripping fences and trailing junk.
      2. **Repair** — if salvage still doesn't validate, send a single
         "fix this JSON" turn that includes the schema and the broken
         payload. Validate again; if still bad, raise so the caller
         (typically the planner retry loop) decides.

    Both stages are transparent: from the node's perspective,
    ``structured()`` either returns a validated model instance or
    raises ``pydantic.ValidationError``.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._endpoint = endpoint or settings.VLLM_ENDPOINT
        self._model = model or settings.VLLM_MODEL
        # ``api_key`` falls back to settings so the env (.env) is the single
        # source of truth. Local plain vLLM accepts any non-empty string.
        resolved_key = api_key or settings.VLLM_API_KEY or "not-needed"
        self._client = AsyncOpenAI(base_url=self._endpoint, api_key=resolved_key)

    @property
    def model(self) -> str:
        return self._model

    async def structured(
        self,
        messages: list[dict[str, Any]],
        response_model: type[T],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> T:
        """Call the model and parse the response into ``response_model``.

        See class docstring for the salvage + repair contract.
        """
        schema = response_model.model_json_schema()
        completion = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": schema,
                    "strict": True,
                },
            },
            temperature=temperature,
            max_tokens=max_tokens,
        )
        raw = completion.choices[0].message.content or ""

        # Stage 1: salvage.
        salvaged = _extract_first_json_object(raw)
        try:
            return response_model.model_validate_json(salvaged)
        except ValidationError as first_err:
            log.warning(
                "structured(%s): salvage failed, attempting repair (err=%s)",
                response_model.__name__,
                first_err.errors()[0].get("type") if first_err.errors() else "?",
            )

        # Stage 2: ask the model to repair its own output. We send the
        # schema explicitly so models that ignore ``response_format``
        # still see the contract in plain text.
        repair_payload = raw[:_REPAIR_PAYLOAD_BYTES]
        repair_messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a JSON repair tool. Output ONLY a single valid "
                    f"JSON object that matches this JSON Schema:\n"
                    f"{json.dumps(schema)}\n"
                    "Do not include explanations, code fences, or any text "
                    "outside the JSON object."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Fix this payload to be valid JSON matching the schema. "
                    "Preserve the user's intent; only fix structural issues "
                    "(missing braces, trailing junk, wrong types).\n\n"
                    f"---\n{repair_payload}\n---"
                ),
            },
        ]
        repair = await self._client.chat.completions.create(
            model=self._model,
            messages=repair_messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": schema,
                    "strict": True,
                },
            },
            temperature=0.0,
            max_tokens=max_tokens,
        )
        repaired_raw = repair.choices[0].message.content or ""
        repaired = _extract_first_json_object(repaired_raw)
        # Let ValidationError propagate this time; the planner retry
        # loop (MAX_PLANNER_ATTEMPTS) will surface it as
        # ``last_validation_error`` and re-prompt with feedback.
        return response_model.model_validate_json(repaired)


_default_client: LLMClient | None = None


def get_llm() -> LLMClient:
    """Return a process-wide LLMClient — created lazily."""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


def set_llm_for_testing(client: LLMClient | None) -> None:
    """Hook so tests can swap the client with a stub."""
    global _default_client
    _default_client = client


__all__ = ["LLMClient", "get_llm", "set_llm_for_testing"]
