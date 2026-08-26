"""Bounded process-local state for SSE fan-out and idempotent commands."""
from __future__ import annotations

import asyncio
import copy
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable


class TooManySubscribers(RuntimeError):
    """The bounded SSE subscriber set is full."""


class IdempotencyConflict(RuntimeError):
    """A caller reused a key for a different request body."""


class IdempotencySaturated(RuntimeError):
    """The bounded idempotency registry has no safe eviction candidate."""


class EventBus:
    """Ring-buffered fan-out bus. Event ids are '<epoch>-<n>' so a client
    resuming with Last-Event-ID from a previous process (different epoch)
    or from beyond the ring gets told to reset instead of silently losing
    events."""

    resume_scope = "process_local"

    def __init__(self, capacity: int = 500, max_clients: int = 128) -> None:
        if capacity < 1 or max_clients < 1:
            raise ValueError("EventBus bounds must be positive")
        self.capacity = capacity
        self.max_clients = max_clients
        # Random per-process identity avoids a false same-epoch resume when two
        # workers start during the same wall-clock second.
        self.epoch: str = secrets.token_hex(8)
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
                # Silent loss would let a client keep rendering stale state.
                # Collapse the subscriber backlog to one explicit reset; the
                # authoritative read endpoints are the recovery path.
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - defensive
                        break
                q.put_nowait(
                    {
                        "id": envelope["id"],
                        "event": "reset_required",
                        "data": {
                            "reason": "subscriber_overflow",
                            "resume_scope": self.resume_scope,
                        },
                    }
                )
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
        if len(self._queues) >= self.max_clients:
            raise TooManySubscribers("SSE subscriber capacity reached")
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

    def discard(self, id: str) -> None:
        self._ids.pop(id, None)


@dataclass
class _IdempotencyEntry:
    fingerprint: str
    future: asyncio.Future
    expires_at: float


class IdempotencyStore:
    """Caller-bound, bounded coalescing registry for accepted mutations.

    Concurrent requests with the same key and body await one operation. A key
    reused with a different body fails closed. Results that were not accepted
    can be returned to concurrent waiters without being retained for a later
    retry.
    """

    def __init__(self, capacity: int = 500, ttl_s: int = 10 * 60) -> None:
        if capacity < 1 or ttl_s < 1:
            raise ValueError("IdempotencyStore bounds must be positive")
        self.capacity = capacity
        self.ttl_s = ttl_s
        self._entries: OrderedDict[tuple[str, str], _IdempotencyEntry] = OrderedDict()
        self._lock = asyncio.Lock()

    def _purge_locked(self, now: float) -> None:
        for compound, entry in list(self._entries.items()):
            if entry.future.done() and entry.expires_at <= now:
                self._entries.pop(compound, None)

    async def run(
        self,
        *,
        principal: str,
        key: str,
        fingerprint: str,
        operation: Callable[[], Awaitable[dict]],
        cache_if: Callable[[dict], bool] = lambda result: True,
    ) -> tuple[dict, bool]:
        """Return ``(result, reused)`` for one idempotent operation."""

        compound = (principal, key)
        owner = False
        async with self._lock:
            now = time.monotonic()
            self._purge_locked(now)
            entry = self._entries.get(compound)
            if entry is not None:
                if entry.fingerprint != fingerprint:
                    raise IdempotencyConflict("idempotency key body mismatch")
                self._entries.move_to_end(compound)
                future = entry.future
            else:
                while len(self._entries) >= self.capacity:
                    evictable = next(
                        (
                            item_key
                            for item_key, item in self._entries.items()
                            if item.future.done()
                        ),
                        None,
                    )
                    if evictable is None:
                        raise IdempotencySaturated("idempotency registry saturated")
                    self._entries.pop(evictable, None)
                future = asyncio.get_running_loop().create_future()
                self._entries[compound] = _IdempotencyEntry(
                    fingerprint=fingerprint,
                    future=future,
                    expires_at=now + self.ttl_s,
                )
                owner = True

        if not owner:
            return copy.deepcopy(await asyncio.shield(future)), True

        try:
            result = await operation()
        except BaseException as exc:
            async with self._lock:
                self._entries.pop(compound, None)
                if not future.done():
                    future.set_exception(exc)
                    # Retrieve it here as well as propagating to avoid an
                    # unobserved-future warning when there were no co-waiters.
                    future.exception()
            raise

        async with self._lock:
            if not future.done():
                future.set_result(copy.deepcopy(result))
            if cache_if(result):
                entry = self._entries.get(compound)
                if entry is not None:
                    entry.expires_at = time.monotonic() + self.ttl_s
                    self._entries.move_to_end(compound)
            else:
                self._entries.pop(compound, None)
        return copy.deepcopy(result), False


class HubState:
    def __init__(self) -> None:
        self.chat: deque[dict] = deque(maxlen=300)
        self.raw: deque[dict] = deque(maxlen=400)
        self.dms: dict[str, deque] = {}
        self.presence: dict[str, dict] = {}
        self.bus = EventBus()
        self.sent = SentIds()
        self.idempotency = IdempotencyStore()
        self.nc = None
        self.js = None
        self.connected = False
        self.last_error: str | None = None
        self.messages = 0
        self.last_seq: int | None = None
        self.replay: dict = {
            "ok": None,
            "complete": None,
            "truncated": False,
            "limit": None,
            "scanned": 0,
            "stream_last_seq": {},
            "took_ms": None,
            "error": None,
            "scope": "startup_backfill",
            "durable_resume": False,
            "ran_at": None,
        }
