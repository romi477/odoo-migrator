"""In-process pub/sub for one SSE connection per page."""

from __future__ import annotations

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


class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self.closed = False
        self.published: deque[tuple[str, dict]] = deque(maxlen=RECENT)

    def subscribe(self) -> queue.Queue:
        subscribed: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(subscribed)

        return subscribed

    def unsubscribe(self, subscribed: queue.Queue) -> None:
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
