"""In-process pub/sub for one SSE connection per page."""

from __future__ import annotations

import queue
import threading
from collections import deque

# A ring, not a log. This list grew for the lifetime of the process —
# three events per callback, five callbacks per module, for every module
# of every migration — and nothing ever read the old entries.
RECENT = 512


class EventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
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

    def publish(self, event: str, data: dict) -> None:
        self.published.append((event, data))
        with self._lock:
            targets = list(self._subscribers)
        for target in targets:
            target.put((event, data))
