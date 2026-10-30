from __future__ import annotations

import httpx

from .base import ChatMessage, Completion, ProviderError


class OpenAICompatProvider:
    """Any server speaking /chat/completions (LM Studio, llama.cpp, vLLM, ...)."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers)

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_schema: dict | None = None,
        max_tokens: int = 700,
        temperature: float = 0.2,
        timeout: float = 180.0,
    ) -> Completion:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if json_schema is not None:
            body["response_format"] = {"type": "json_object"}
        try:
            r = await self._client.post("/chat/completions", json=body, timeout=timeout)
        except httpx.TimeoutException as e:
            raise ProviderError(f"{self.base_url} timed out after {timeout:.0f}s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.base_url} unreachable: {e}") from e
        if r.status_code in (401, 403):
            raise ProviderError(f"{self.base_url} rejected the API key", retryable=False)
        if r.status_code >= 400:
            raise ProviderError(f"{self.base_url} HTTP {r.status_code}: {r.text[:200]}",
                                retryable=r.status_code >= 500 or r.status_code == 429)
        try:
            data = r.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(f"{self.base_url} returned an unexpected payload") from e
        usage = data.get("usage") or {}
        return Completion(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
