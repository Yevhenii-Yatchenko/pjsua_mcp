"""Thread-safe pub/sub event bus for scenario engine.

Events are emitted from any thread (PJSUA callbacks, SIP log parser, timers, actions).
Subscribers receive events synchronously on the emitting thread; they must be fast
or schedule their own work via `loop.call_soon_threadsafe` / `asyncio.Future`.

Event-type matching supports:
- exact: "call.state.confirmed"
- prefix wildcard: "call.state.*" (matches call.state.confirmed, call.state.early, ...)
- full wildcard: "*" (matches everything)
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count
from typing import Any


@dataclass
class Event:
    """A single event on the bus.

    `type` uses dot-separated hierarchy: "call.state.confirmed", "sip.request.in".
    `data` carries event-specific payload (status_code, digit, headers, ...).
    """

    type: str
    timestamp: float = field(default_factory=time.monotonic)
    phone_id: str | None = None
    call_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "timestamp": self.timestamp,
            "phone_id": self.phone_id,
            "call_id": self.call_id,
            "data": dict(self.data),
        }


@dataclass
class Subscription:
    sub_id: int
    patterns: list[str]
    callback: Callable[[Event], None]
    once: bool
    active: bool = True


def _matches(sub_patterns: list[str], event_type: str) -> bool:
    for p in sub_patterns:
        if p == "*":
            return True
        if p.endswith(".*"):
            prefix = p[:-2]
            if event_type == prefix or event_type.startswith(prefix + "."):
                return True
        elif p == event_type:
            return True
    return False


class EventBus:
    """Thread-safe pub/sub hub."""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._lock = threading.Lock()
        self._subs: dict[int, Subscription] = {}
        self._ids = count(1)
        self._loop = loop

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(
        self,
        event_type: str | list[str],
        callback: Callable[[Event], None],
        once: bool = False,
    ) -> int:
        patterns = [event_type] if isinstance(event_type, str) else list(event_type)
        sub = Subscription(
            sub_id=next(self._ids),
            patterns=patterns,
            callback=callback,
            once=once,
        )
        with self._lock:
            self._subs[sub.sub_id] = sub
        return sub.sub_id

    def unsubscribe(self, sub_id: int) -> None:
        with self._lock:
            sub = self._subs.pop(sub_id, None)
        if sub is not None:
            sub.active = False

    def emit(self, event: Event) -> None:
        with self._lock:
            subs = [s for s in self._subs.values() if s.active and _matches(s.patterns, event.type)]
        to_remove: list[int] = []
        for sub in subs:
            try:
                sub.callback(event)
            except Exception as exc:  # noqa: BLE001
                # Subscriber failure must not break other subscribers.
                print(f"[EventBus] subscriber {sub.sub_id} on {event.type} raised: {exc!r}")
            if sub.once:
                to_remove.append(sub.sub_id)
        if to_remove:
            with self._lock:
                for sid in to_remove:
                    self._subs.pop(sid, None)

    async def wait_for(
        self,
        event_type: str | list[str],
        predicate: Callable[[Event], bool] | None = None,
        timeout: float = 10.0,
    ) -> Event:
        """Async helper: await first event matching type (+ optional predicate)."""
        loop = self._loop or asyncio.get_running_loop()
        future: asyncio.Future[Event] = loop.create_future()

        def handler(ev: Event) -> None:
            if predicate is not None and not predicate(ev):
                return
            if future.done():
                return
            loop.call_soon_threadsafe(future.set_result, ev)

        sub_id = self.subscribe(event_type, handler, once=False)
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.unsubscribe(sub_id)

    def snapshot_subscribers(self) -> list[dict[str, Any]]:
        """For debugging — list active subscriptions."""
        with self._lock:
            return [
                {"id": s.sub_id, "patterns": list(s.patterns), "once": s.once}
                for s in self._subs.values()
                if s.active
            ]


# ---- Module-level "default bus" — set by server.py at startup ----
# This lets pjsua callbacks (AccountManager, CallManager, SipLogger) emit
# events without needing a bus reference threaded through every constructor.
# When no bus is set, emit_global() is a no-op — the engine is optional.

_default_bus: EventBus | None = None


def set_default_bus(bus: EventBus | None) -> None:
    global _default_bus
    _default_bus = bus


def get_default_bus() -> EventBus | None:
    return _default_bus


def emit_global(event: Event) -> None:
    """Emit an event to the default bus if one is set; no-op otherwise."""
    if _default_bus is not None:
        _default_bus.emit(event)
