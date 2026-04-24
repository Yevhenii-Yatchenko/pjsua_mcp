"""Timeline recorder — subscribes to event bus and collects chronological log.

Timeline is the primary output of `run_scenario`: LLM reads it to understand
what actually happened (vs expected_timeline) and diagnose failures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.scenario_engine.event_bus import Event, EventBus


@dataclass
class TimelineEntry:
    """One row in the scenario timeline.

    kind: "event" (from bus) or "action" (executed by engine) or "meta" (scenario lifecycle).
    """

    kind: str
    ts: float
    ts_offset_ms: float
    type: str
    phone_id: str | None = None
    call_id: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    hook_id: str | None = None
    pattern_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ts": self.ts,
            "ts_offset_ms": round(self.ts_offset_ms, 2),
            "type": self.type,
            "phone_id": self.phone_id,
            "call_id": self.call_id,
            "data": dict(self.data),
            "hook_id": self.hook_id,
            "pattern_name": self.pattern_name,
        }


class Timeline:
    """In-memory chronological log of a scenario run."""

    def __init__(self, t0: float | None = None) -> None:
        self.t0: float = t0 if t0 is not None else time.monotonic()
        self.entries: list[TimelineEntry] = []

    def add(self, entry: TimelineEntry) -> None:
        self.entries.append(entry)

    def to_list(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.entries]

    def find(self, type: str, phone_id: str | None = None) -> list[TimelineEntry]:
        return [
            e for e in self.entries
            if e.type == type and (phone_id is None or e.phone_id == phone_id)
        ]

    def has_any(self, type: str, phone_id: str | None = None) -> bool:
        return bool(self.find(type, phone_id))


class TimelineRecorder:
    """Subscribes to all events, appends to a Timeline."""

    def __init__(self, bus: EventBus, timeline: Timeline) -> None:
        self._bus = bus
        self._timeline = timeline
        self._sub_id: int | None = None

    def start(self) -> None:
        if self._sub_id is not None:
            return
        self._sub_id = self._bus.subscribe("*", self._on_event)

    def stop(self) -> None:
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None

    def record_action(
        self,
        action: str,
        phone_id: str | None = None,
        call_id: int | None = None,
        data: dict[str, Any] | None = None,
        hook_id: str | None = None,
        pattern_name: str | None = None,
    ) -> None:
        ts = time.monotonic()
        self._timeline.add(
            TimelineEntry(
                kind="action",
                ts=ts,
                ts_offset_ms=(ts - self._timeline.t0) * 1000,
                type=action,
                phone_id=phone_id,
                call_id=call_id,
                data=dict(data or {}),
                hook_id=hook_id,
                pattern_name=pattern_name,
            )
        )

    def record_meta(self, type: str, data: dict[str, Any] | None = None) -> None:
        ts = time.monotonic()
        self._timeline.add(
            TimelineEntry(
                kind="meta",
                ts=ts,
                ts_offset_ms=(ts - self._timeline.t0) * 1000,
                type=type,
                data=dict(data or {}),
            )
        )

    def _on_event(self, ev: Event) -> None:
        self._timeline.add(
            TimelineEntry(
                kind="event",
                ts=ev.timestamp,
                ts_offset_ms=(ev.timestamp - self._timeline.t0) * 1000,
                type=ev.type,
                phone_id=ev.phone_id,
                call_id=ev.call_id,
                data=dict(ev.data),
            )
        )
