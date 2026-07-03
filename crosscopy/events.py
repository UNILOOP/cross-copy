"""Tiny thread-safe pub/sub event bus for cross-copy (v0.3).

Anything that changes daemon state publishes a typed event; the server's
/api/events SSE endpoint fans them out to connected clients. Events are
payload-free `{"type": "clipboard" | "peers" | "update"}` dicts — clients
refetch /api/status or /api/peers on receipt.
"""

import queue
import threading


class EventBus:
    """Fan-out bus: each subscriber gets its own bounded Queue."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []          # list of (queue, tag) pairs

    def subscribe(self, maxsize: int = 64, tag: str = None) -> "queue.Queue":
        """Register and return a new subscriber queue.

        `tag` labels the client type (e.g. "widget") so other components can
        ask whether such a client is currently connected (has_client)."""
        q = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append((q, tag))
        return q

    def unsubscribe(self, q) -> None:
        """Remove a subscriber queue (no-op if already removed)."""
        with self._lock:
            self._subscribers = [(sq, st) for (sq, st) in self._subscribers
                                 if sq is not q]

    def has_client(self, tag: str) -> bool:
        """True if at least one subscriber registered with this tag."""
        with self._lock:
            return any(st == tag for (_, st) in self._subscribers)

    def publish(self, event_type: str) -> None:
        """Deliver {"type": event_type} to every subscriber (never blocks;
        events are dropped for subscribers with a full queue)."""
        event = {"type": event_type}
        with self._lock:
            subscribers = [sq for (sq, _) in self._subscribers]
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


# Shared process-wide bus used by the server, discovery, and updater.
bus = EventBus()
