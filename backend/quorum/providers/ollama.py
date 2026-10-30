from __future__ import annotations

import httpx

from .base import ChatMessage, Completion, ProviderError


class OllamaProvider:
    def __init__(self, base_url: str, tag: str):
        self.base_url = base_url.rstrip("/")
        self.tag = tag
        self._client = httpx.AsyncClient(base_url=self.base_url)

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
            "model": self.tag,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_schema is not None:
            body["format"] = json_schema  # Ollama accepts a JSON schema here
        try:
            r = await self._client.post("/api/chat", json=body, timeout=timeout)
        except httpx.TimeoutException as e:
            raise ProviderError(f"ollama timed out after {timeout:.0f}s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"ollama unreachable at {self.base_url}: {e}") from e
        if r.status_code == 404:
            raise ProviderError(f"model '{self.tag}' is not pulled (ollama pull {self.tag})",
                                retryable=False)
        if r.status_code >= 400:
            raise ProviderError(f"ollama HTTP {r.status_code}: {r.text[:200]}",
                                retryable=r.status_code >= 500)
        data = r.json()
        return Completion(
            text=(data.get("message") or {}).get("content", ""),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


async def list_ollama_models(base_url: str, timeout: float = 3.0) -> list[dict]:
    """Return Ollama's /api/tags entries, or raise ProviderError if unreachable."""
    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/")) as c:
            r = await c.get("/api/tags", timeout=timeout)
            r.raise_for_status()
            return r.json().get("models", [])
    except (httpx.HTTPError, ValueError) as e:
        raise ProviderError(f"ollama unreachable at {base_url}: {e}") from e


async def ollama_version(base_url: str, timeout: float = 2.0) -> str | None:
    try:
        async with httpx.AsyncClient(base_url=base_url.rstrip("/")) as c:
            r = await c.get("/api/version", timeout=timeout)
            return r.json().get("version")
    except (httpx.HTTPError, ValueError):
        return None
