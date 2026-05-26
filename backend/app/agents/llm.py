from __future__ import annotations

from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import settings

T = TypeVar("T", bound=BaseModel)


class LLMClient:
    """Thin wrapper around the OpenAI-compatible vLLM endpoint.

    Every structured call passes a Pydantic-derived JSON Schema as
    ``response_format`` so vLLM's ``xgrammar`` backend constrains
    decoding. We never parse free-text JSON from the model.
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
        """Call the model and parse the response into ``response_model``."""
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
        text = completion.choices[0].message.content or ""
        return response_model.model_validate_json(text)


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
