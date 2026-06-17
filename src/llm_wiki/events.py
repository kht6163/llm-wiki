"""In-process pub/sub hub bridging worker-thread publishers to async WebSocket
subscribers, so live document changes can be pushed to connected browsers.

Writes run in a threadpool (sync FastAPI handlers) or an anyio worker thread (MCP
tools), never on the event loop. ``publish`` is therefore thread-safe and
non-blocking: it hands each event to the bound loop via ``call_soon_threadsafe``.
Subscribers are per-connection ``asyncio.Queue`` objects drained on the loop. If no
loop is bound (e.g. a CLI ``reindex`` with no server running), publish is a no-op,
and a full queue drops the event rather than blocking a write.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from .metrics import WS_EVENTS_DROPPED, WS_SUBSCRIBERS

log = logging.getLogger("llm_wiki.events")
_last_drop_log = 0.0  # monotonic time of the last throttled "queue full" warning


class EventHub:
    def __init__(self, max_queue: int = 200):
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the event loop that owns the subscriber queues. Idempotent; called
        lazily when the first WebSocket connects."""
        with self._lock:
            self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subs.add(q)
            WS_SUBSCRIBERS.set(len(self._subs))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(q)
            WS_SUBSCRIBERS.set(len(self._subs))

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, event: dict) -> None:
        """Fan an event out to all subscribers. Safe to call from any thread; never
        raises (notification must not be able to break a write)."""
        with self._lock:
            loop = self._loop
            subs = list(self._subs)
        if loop is None or not subs:
            return
        for q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:
                # Loop is closed/closing (e.g. during shutdown) — drop silently.
                pass

    @staticmethod
    def _offer(q: asyncio.Queue, event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # A slow/stuck client must not stall the loop; drop the event for it. Count
            # every drop (the rate is the real signal) and warn at most once per 5s so a
            # wedged client can't flood the log.
            global _last_drop_log
            WS_EVENTS_DROPPED.inc()
            now = time.monotonic()
            if now - _last_drop_log > 5.0:
                _last_drop_log = now
                log.warning("realtime event dropped: a subscriber queue is full (slow client)")
