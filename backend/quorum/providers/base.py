from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

ChatMessage = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}


class ProviderError(RuntimeError):
    """A model call failed in a way worth reporting (timeout, HTTP error, bad payload)."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(Protocol):
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_schema: dict | None = None,
        max_tokens: int = 700,
        temperature: float = 0.2,
        timeout: float = 180.0,
    ) -> Completion: ...

    async def aclose(self) -> None: ...
