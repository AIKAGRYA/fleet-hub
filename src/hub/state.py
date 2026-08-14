"""In-memory hub state: SSE event bus with resume, echo LRU, and HubState."""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque


class EventBus:
    """Ring-buffered fan-out bus. Event ids are '<epoch>-<n>' so a client
    resuming with Last-Event-ID from a previous process (different epoch)
    or from beyond the ring gets told to reset instead of silently losing
    events."""

    def __init__(self, capacity: int = 500) -> None:
        self.capacity = capacity
        self.epoch: str = str(int(time.time()))
        self.n = 0
        self._ring: deque[dict] = deque(maxlen=capacity)
        self._queues: list[asyncio.Queue] = []

    def publish(self, kind: str, payload: dict) -> dict:
        self.n += 1
        envelope = {"id": f"{self.epoch}-{self.n}", "event": kind, "data": payload}
        self._ring.append(envelope)
        for q in list(self._queues):
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                pass
        return envelope

    def since(self, last_event_id: str | None) -> tuple[list[dict], bool]:
        """(backlog, needs_reset). Unknown/foreign ids and ids that fell off
        the ring return ([], True) — the client must refetch state."""
        if not last_event_id:
            return [], False
        try:
            epoch, n_str = last_event_id.rsplit("-", 1)
            n = int(n_str)
        except ValueError:
            return [], True
        if epoch != self.epoch or n > self.n:
            return [], True
        if not self._ring:
            return [], False
        oldest_n = int(self._ring[0]["id"].rsplit("-", 1)[1])
        if n < oldest_n - 1:
            return [], True
        backlog = [
            e for e in self._ring if int(e["id"].rsplit("-", 1)[1]) > n
        ]
        return backlog, False

    def attach(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.capacity)
        self._queues.append(q)
        return q

    def detach(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def clients(self) -> int:
        return len(self._queues)


class SentIds:
    """LRU set of operator-sent msg_ids, used to suppress our own NATS echo."""

    def __init__(self, cap: int = 200) -> None:
        self.cap = cap
        self._ids: OrderedDict[str, None] = OrderedDict()

    def add(self, id: str) -> None:
        self._ids[id] = None
        self._ids.move_to_end(id)
        while len(self._ids) > self.cap:
            self._ids.popitem(last=False)

    def __contains__(self, id: object) -> bool:
        return id in self._ids


class HubState:
    def __init__(self) -> None:
        self.chat: deque[dict] = deque(maxlen=300)
        self.raw: deque[dict] = deque(maxlen=400)
        self.dms: dict[str, deque] = {}
        self.presence: dict[str, dict] = {}
        self.bus = EventBus()
        self.sent = SentIds()
        self.nc = None
        self.js = None
        self.connected = False
        self.last_error: str | None = None
        self.messages = 0
        self.last_seq: int | None = None
        self.replay: dict = {
            "ok": None,
            "scanned": 0,
            "took_ms": None,
            "error": None,
            "ran_at": None,
        }
