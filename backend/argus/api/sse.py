"""Server-sent event stream plumbing: bounded per-stream buffers and shutdown.

An SSE response only ends on a terminal event or a client disconnect, so open streams used to
keep uvicorn waiting forever on SIGTERM until Docker killed the process (and with it the
lifespan shutdown that records final run statuses). ``SseHub`` tracks every open stream and ends
them all at once when shutdown begins; ``chain_shutdown_signals`` makes that happen as soon as the
signal arrives, before uvicorn starts waiting for connections to close. Clients reconnect with
``Last-Event-ID`` and resume where they left off.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
from collections.abc import Callable, Iterable
from typing import Any

from argus.core.logging import get_logger

log = get_logger(__name__)

STREAM_QUEUE_SIZE = 1000
END: Any = object()  # the stream must end (shutdown, or a durable frame could not be buffered)
HEARTBEAT: Any = object()  # nothing arrived in time: send a keep-alive


class StreamBuffer:
    """Bounded frames waiting for one SSE client.

    When the client stalls and the buffer fills, ephemeral frames (deltas, budget ticks) are
    dropped: durable events carry their content anyway. A durable frame is never dropped
    silently: the stream ends instead and the client resumes from its last event id.
    """

    def __init__(self, maxsize: int) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue(maxsize)
        self.ended = False

    def offer(self, frame: Any, *, ephemeral: bool) -> bool:
        """Queue a frame (never ``None``); returns False once the stream has to end."""
        if self.ended:
            return False
        try:
            self._q.put_nowait(frame)
        except asyncio.QueueFull:
            if ephemeral:
                return True
            self.ended = True
            return False
        return True

    def close(self) -> None:
        self.ended = True
        with contextlib.suppress(asyncio.QueueFull):
            self._q.put_nowait(None)  # wakes a waiting reader; a full queue is drained first anyway

    async def get(self, idle_s: float) -> Any:
        """Next frame, ``HEARTBEAT`` after ``idle_s`` seconds without one, or ``END``."""
        if self.ended and self._q.empty():
            return END
        try:
            item = await asyncio.wait_for(self._q.get(), idle_s)
        except TimeoutError:
            return END if self.ended else HEARTBEAT
        return END if item is None else item


class SseHub:
    """Open SSE streams of this process."""

    def __init__(self, maxsize: int = STREAM_QUEUE_SIZE) -> None:
        self.maxsize = maxsize
        self.buffers: set[StreamBuffer] = set()
        self.shutting_down = False

    def open(self) -> StreamBuffer:
        buf = StreamBuffer(self.maxsize)
        if self.shutting_down:
            buf.close()
        self.buffers.add(buf)
        return buf

    def release(self, buf: StreamBuffer) -> None:
        self.buffers.discard(buf)

    def shutdown(self) -> None:
        if not self.shutting_down:
            log.info("sse.shutdown", open_streams=len(self.buffers))
        self.shutting_down = True
        for buf in list(self.buffers):
            buf.close()


def chain_shutdown_signals(
    callback: Callable[[], None], signals: Iterable[int] = (signal.SIGTERM, signal.SIGINT)
) -> Callable[[], None]:
    """Run ``callback`` on the event loop when a shutdown signal arrives, then hand the signal on.

    Wraps the handler already installed (uvicorn's), so the server's own shutdown is unchanged.
    Signals that have no Python handler are left alone. Returns a function that restores the
    previous handlers.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    loop = asyncio.get_running_loop()
    installed: dict[int, tuple[Any, Any]] = {}
    for sig in signals:
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue

        def handler(signum: int, frame: Any, previous: Any = previous) -> None:
            loop.call_soon_threadsafe(callback)
            previous(signum, frame)

        signal.signal(sig, handler)
        installed[sig] = (handler, previous)

    def restore() -> None:
        for sig, (handler, previous) in installed.items():
            if signal.getsignal(sig) is handler:
                signal.signal(sig, previous)

    return restore
