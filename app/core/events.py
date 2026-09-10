"""A tiny in-process pub/sub for pushing dashboard updates to the browser
over SSE instead of it polling.

The crawler runs in its own thread with its own asyncio loop, so it can't
touch the server loop's asyncio primitives directly. `publish()` is
therefore safe to call from any thread: it hands the event to the server
loop via `call_soon_threadsafe`. Subscribers (the SSE endpoint, on the
server loop) each get their own bounded queue.

Only meaningful with a single server process. That's what we run
(one uvicorn worker); with multiple workers this would need an external
broker and is out of scope.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("proxy.events")

# Max queued events per subscriber before we drop the oldest. A slow client
# should fall behind and lose intermediate frames, never make publishers
# block or blow up memory.
_QUEUE_MAXSIZE = 100


# Sentinel pushed to every subscriber queue on shutdown so the SSE
# generators can end promptly instead of blocking the graceful-shutdown
# window waiting on their keepalive timeout.
SHUTDOWN = object()


class EventBroker:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: set[asyncio.Queue] = set()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the server's startup, on the server event loop."""
        self._loop = loop

    def shutdown(self) -> None:
        """Signal all subscribers to stop. Runs on the server loop."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(SHUTDOWN)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event_type: str, data: Any) -> None:
        """Thread-safe. Serialises the event and schedules delivery on the
        server loop. A no-op if nobody is listening or the loop isn't up.
        """
        loop = self._loop
        if loop is None or not self._subscribers:
            return
        try:
            payload = json.dumps({"type": event_type, "data": data})
        except (TypeError, ValueError):
            logger.exception("event payload not serialisable: %s", event_type)
            return
        try:
            loop.call_soon_threadsafe(self._fan_out, payload)
        except RuntimeError:
            # Loop is shutting down -- nothing to deliver to anyway.
            pass

    def _fan_out(self, payload: str) -> None:
        """Runs on the server loop."""
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest so the newest still gets through.
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass


# Process-wide singleton -- imported by the crawler (publisher) and the
# SSE endpoint (subscriber).
broker = EventBroker()
