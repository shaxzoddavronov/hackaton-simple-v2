from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.metrics import llm_calls_total

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
      - **Truncated JSON** that ran out of ``max_tokens`` mid-string
        or mid-object. We close any open string with a ``"`` and any
        open object/array with ``}``/``]`` so the result at least
        parses. The user gets a partial answer rather than a crash.

    The function walks the string once tracking brace depth and string
    state. If it finds a balanced top-level ``{…}`` it returns that
    substring. Otherwise it appends the minimal closers needed to
    balance and returns the patched payload.
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

    # Drop everything before the first '{' so a chatty preamble can't
    # confuse the closer count below.
    first_brace = text.find("{")
    if first_brace < 0:
        return text
    if first_brace > 0:
        text = text[first_brace:]

    depth = 0
    bracket_depth = 0
    in_str = False
    escape = False
    # Stack of openers in order so we can close them in reverse.
    opener_stack: list[str] = []

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
            depth += 1
            opener_stack.append("}")
        elif ch == "}":
            depth -= 1
            if opener_stack and opener_stack[-1] == "}":
                opener_stack.pop()
            if depth == 0 and bracket_depth == 0:
                # Found a balanced top-level object — clip and return.
                return text[: i + 1]
        elif ch == "[":
            bracket_depth += 1
            opener_stack.append("]")
        elif ch == "]":
            bracket_depth -= 1
            if opener_stack and opener_stack[-1] == "]":
                opener_stack.pop()

    # Unbalanced — patch it. Close any open string, then close every
    # outstanding bracket/brace in LIFO order. Strip a trailing comma
    # before closing if present (common with truncated arrays).
    patched = text
    if in_str:
        patched += '"'
    # If the LAST non-whitespace char before truncation is ',' or ':'
    # it'll make the JSON invalid — backfill with `null` for `:` and
    # drop the `,`.
    patched = re.sub(r",\s*$", "", patched)
    if re.search(r":\s*$", patched):
        patched += " null"
    for closer in reversed(opener_stack):
        patched += closer
    return patched


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
        # Use the response model name as the "node" label — it's the
        # closest stable identifier we have without dragging a node
        # name through every call site (and IntentDecision / SqlPlan /
        # AnswerDraft map 1:1 to coordinator / planner / answer_writer
        # anyway).
        node_label = response_model.__name__
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

        # Phase 37 — record token usage for the per-workspace dashboard.
        # The usage bucket is a ContextVar set by ``api/chat.py``; when
        # this LLM call happens outside a request (unit tests, batch
        # jobs), ``record_llm`` is a no-op.
        try:
            from app.services.usage import record_llm

            usage_obj = getattr(completion, "usage", None)
            in_tok = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
            out_tok = int(getattr(usage_obj, "completion_tokens", 0) or 0)
            record_llm(in_tok, out_tok)
        except Exception:  # pragma: no cover — never break the chat
            pass

        # Stage 1: salvage.
        salvaged = _extract_first_json_object(raw)
        try:
            parsed = response_model.model_validate_json(salvaged)
            llm_calls_total.labels(node=node_label, outcome="ok").inc()
            return parsed
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
        try:
            parsed_repaired = response_model.model_validate_json(repaired)
            llm_calls_total.labels(node=node_label, outcome="repair").inc()
            return parsed_repaired
        except ValidationError:
            llm_calls_total.labels(node=node_label, outcome="failed").inc()
            raise


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
