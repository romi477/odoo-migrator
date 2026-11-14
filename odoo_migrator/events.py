"""In-process pub/sub for one SSE connection per page."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections import deque

# A ring, not a log. This list grew for the lifetime of the process —
# three events per callback, five callbacks per module, for every module
# of every migration — and nothing ever read the old entries.
RECENT = 512


# What a subscriber is handed when the app is going down. A stream parked
# on a blocking get has to be woken, not waited out.
CLOSED = object()


class _LoopQueue(asyncio.Queue):
    """A subscriber that waits on its event loop. `put` is called from any
    thread, and hands the item to the loop that owns the queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self._owner = loop

    def put(self, item) -> None:  # type: ignore[override]
        try:
            self._owner.call_soon_threadsafe(self.put_nowait, item)
        except RuntimeError:
            # The loop is gone: nobody is reading, and the stream's own
            # `finally` unsubscribes it when it can.
            pass


class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue | _LoopQueue] = []
        self.closed = False
        self.published: deque[tuple[str, dict]] = deque(maxlen=RECENT)

    def subscribe(self) -> queue.Queue:
        subscribed: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(subscribed)

        return subscribed

    def subscribe_async(self) -> asyncio.Queue:
        """For a stream on the event loop: `await get()` takes no thread, where
        a blocking get through `asyncio.to_thread` held one of the default
        executor's per open page. Call it on the loop that reads it."""
        subscribed = _LoopQueue(asyncio.get_running_loop())
        with self._lock:
            self._subscribers.append(subscribed)
            closed = self.closed
        if closed:
            subscribed.put_nowait(CLOSED)

        return subscribed

    def unsubscribe(self, subscribed: queue.Queue | asyncio.Queue) -> None:
        with self._lock:
            if subscribed in self._subscribers:
                self._subscribers.remove(subscribed)

    def close(self) -> None:
        """Wake every open stream so the process can exit.

        Without this, SIGTERM freed the port and the process stayed up:
        each SSE generator sat in a blocking queue read and only gave up
        when its own five-minute window ran out, so shutdown took minutes
        or looked like a hang and needed SIGKILL.
        """
        with self._lock:
            self.closed = True
            targets = list(self._subscribers)
        for target in targets:
            target.put(CLOSED)

    def publish(self, event: str, data: dict) -> None:
        self.published.append((event, data))
        with self._lock:
            targets = list(self._subscribers)
        for target in targets:
            target.put((event, data))
