"""Fan one browser session's live frames out to every viewer in this process.

The browser service streams frames per request, so each watching tab would otherwise cost its own
upstream stream (and screencast). ``FrameRelay`` keeps one upstream subscription per session: it
starts with the first viewer and stops ``grace_s`` after the last one leaves. Each viewer holds at
most ``VIEWER_BUFFER`` frames; when a slow viewer's buffer is full the oldest frame is dropped, so a
stalled client costs a stale picture, never memory.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from argus.core.logging import get_logger

log = get_logger(__name__)

VIEWER_BUFFER = 2
GRACE_S = 5.0


class Viewer:
    def __init__(self) -> None:
        self.buffer: deque[bytes] = deque(maxlen=VIEWER_BUFFER)  # a full deque drops its oldest frame
        self.ended = False
        self._wake = asyncio.Event()

    def push(self, frame: bytes) -> None:
        self.buffer.append(frame)
        self._wake.set()

    def end(self) -> None:
        self.ended = True
        self._wake.set()

    async def get(self, wait_s: float | None = None) -> bytes | None:
        """The next frame; ``None`` when the stream ended or ``wait_s`` passed with nothing new."""
        while True:
            if self.buffer:
                return self.buffer.popleft()
            if self.ended:
                return None
            self._wake.clear()
            try:
                async with asyncio.timeout(wait_s):
                    await self._wake.wait()
            except TimeoutError:
                return None


class _Feed:
    def __init__(self) -> None:
        self.viewers: set[Viewer] = set()
        self.task: asyncio.Task[None] | None = None
        self.stop_timer: asyncio.TimerHandle | None = None


class FrameRelay:
    def __init__(self, browser: Any, max_fps: int, *, grace_s: float = GRACE_S) -> None:
        self.browser = browser
        self.max_fps = max_fps
        self.grace_s = grace_s
        self._feeds: dict[str, _Feed] = {}

    def active(self) -> int:
        return len(self._feeds)

    @contextlib.asynccontextmanager
    async def watch(self, session_id: str) -> AsyncIterator[Viewer]:
        viewer = Viewer()
        feed = self._feeds.get(session_id)
        if feed is None:
            feed = self._feeds[session_id] = _Feed()
        if feed.stop_timer is not None:
            feed.stop_timer.cancel()
            feed.stop_timer = None
        feed.viewers.add(viewer)
        if feed.task is None:
            feed.task = asyncio.create_task(self._pump(session_id, feed))
        try:
            yield viewer
        finally:
            feed.viewers.discard(viewer)
            if not feed.viewers and feed.stop_timer is None:
                loop = asyncio.get_running_loop()
                feed.stop_timer = loop.call_later(self.grace_s, self._stop, session_id, feed)

    def _stop(self, session_id: str, feed: _Feed) -> None:
        feed.stop_timer = None
        if feed.viewers:
            return
        if self._feeds.get(session_id) is feed:
            del self._feeds[session_id]
        if feed.task is not None:
            feed.task.cancel()

    async def _pump(self, session_id: str, feed: _Feed) -> None:
        frames = self.browser.frames(session_id, self.max_fps)
        try:
            async for frame in frames:
                for viewer in tuple(feed.viewers):
                    viewer.push(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # session gone, service down: viewers end and may reconnect
            log.info("browser.frames_ended", error=type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                await frames.aclose()
            # Upstream over (it ended, failed or was stopped): the next viewer starts a fresh one.
            if self._feeds.get(session_id) is feed:
                del self._feeds[session_id]
            for viewer in tuple(feed.viewers):
                viewer.end()

    async def shutdown(self) -> None:
        tasks = []
        for feed in list(self._feeds.values()):
            if feed.stop_timer is not None:
                feed.stop_timer.cancel()
            if feed.task is not None:
                feed.task.cancel()
                tasks.append(feed.task)
        await asyncio.gather(*tasks, return_exceptions=True)


def relay_factory(settings: Any) -> Callable[[], FrameRelay | None]:
    def make() -> FrameRelay | None:
        if not settings.browser_url:
            return None
        from argus.browser.client import RemoteBrowser

        token = settings.browser_token.get_secret_value() if settings.browser_token else ""
        return FrameRelay(RemoteBrowser(settings.browser_url, token), settings.browser_frame_fps)

    return make
