"""Provider interface, error taxonomy and retry policy."""

from __future__ import annotations

import abc
import asyncio
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

from argus.providers.types import CompletionRequest, CompletionResponse, StreamEvent


class ProviderError(Exception):
    """Raised by providers. ``retryable`` drives retries and fallback routing."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after

    @classmethod
    def from_status(cls, provider: str, status: int, body: str, retry_after: float | None = None):
        retryable = status in (408, 409, 425, 429) or status >= 500
        # Never echo upstream bodies verbatim: they may contain request content.
        return cls(
            f"{provider} returned HTTP {status}: {body[:300]}",
            provider=provider,
            status=status,
            retryable=retryable,
            retry_after=retry_after,
        )


class Provider(abc.ABC):
    """One configured connection to a model backend."""

    kind: str = "base"
    # Set by the router for providers built from a user's ProviderConfig (health tracking).
    config_id: uuid.UUID | None = None

    def __init__(self, name: str | None = None) -> None:
        self.name = name or self.kind

    @abc.abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """Default: non-streaming completion surfaced as a single burst."""
        response = await self.complete(request)
        if response.thinking:
            yield StreamEvent(type="thinking", text=response.thinking)
        if response.content:
            yield StreamEvent(type="text", text=response.content)
        for call in response.tool_calls:
            yield StreamEvent(type="tool_call", tool_call=call)
        yield StreamEvent(type="done", response=response)

    async def list_models(self) -> list[str]:
        return []

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        pass


async def with_retries[T](
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 20.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Retry ``fn`` on retryable ProviderErrors with jittered exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except ProviderError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            await sleep(retry_delay(attempt, exc, base_delay=base_delay, max_delay=max_delay))
    raise AssertionError("unreachable")


def retry_delay(
    attempt: int, exc: ProviderError, *, base_delay: float = 0.5, max_delay: float = 20.0
) -> float:
    """Backoff before retry ``attempt + 1``: ``Retry-After`` if given, else exponential; plus jitter."""
    delay = exc.retry_after or min(max_delay, base_delay * 2 ** (attempt - 1))
    return delay + random.uniform(0, delay / 4)  # noqa: S311
