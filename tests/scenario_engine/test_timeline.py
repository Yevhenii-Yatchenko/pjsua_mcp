"""Unit tests for TimelineRecorder — events + actions + meta with correct offsets."""

from __future__ import annotations

import time

from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.timeline import Timeline, TimelineEntry, TimelineRecorder


def test_timeline_starts_empty() -> None:
    tl = Timeline()
    assert tl.entries == []
    assert tl.to_list() == []


def test_recorder_subscribes_and_records_events() -> None:
    bus = EventBus()
    tl = Timeline()
    rec = TimelineRecorder(bus, tl)
    rec.start()
    bus.emit(Event(type="call.state.confirmed", phone_id="a", call_id=1))
    bus.emit(Event(type="dtmf.in", phone_id="b", call_id=2, data={"digit": "1"}))
    rec.stop()

    types = [e.type for e in tl.entries]
    assert types == ["call.state.confirmed", "dtmf.in"]
    assert all(e.kind == "event" for e in tl.entries)
    assert tl.entries[1].data == {"digit": "1"}


def test_recorder_stop_detaches_subscriber() -> None:
    bus = EventBus()
    tl = Timeline()
    rec = TimelineRecorder(bus, tl)
    rec.start()
    bus.emit(Event(type="a"))
    rec.stop()
    bus.emit(Event(type="b"))  # should NOT be recorded
    types = [e.type for e in tl.entries]
    assert types == ["a"]


def test_record_action_emits_entry_with_action_kind() -> None:
    bus = EventBus()
    tl = Timeline()
    rec = TimelineRecorder(bus, tl)
    rec.record_action("answer", phone_id="a", call_id=1, data={"status_code": 200},
                      hook_id="h1", pattern_name="auto-answer")
    assert len(tl.entries) == 1
    e = tl.entries[0]
    assert e.kind == "action"
    assert e.type == "answer"
    assert e.data == {"status_code": 200}
    assert e.hook_id == "h1"
    assert e.pattern_name == "auto-answer"


def test_record_meta_emits_entry_with_meta_kind() -> None:
    bus = EventBus()
    tl = Timeline()
    rec = TimelineRecorder(bus, tl)
    rec.record_meta("checkpoint", {"label": "start"})
    assert tl.entries[0].kind == "meta"
    assert tl.entries[0].data == {"label": "start"}


def test_offsets_are_monotonic_from_t0() -> None:
    """ts_offset_ms should be non-negative and increase across entries."""
    t0 = time.monotonic()
    tl = Timeline(t0=t0)
    bus = EventBus()
    rec = TimelineRecorder(bus, tl)
    rec.start()
    rec.record_meta("first", {})
    time.sleep(0.02)
    rec.record_meta("second", {})
    rec.stop()

    offsets = [e.ts_offset_ms for e in tl.entries]
    assert offsets[0] >= 0
    assert offsets[1] > offsets[0]
    assert 10 < (offsets[1] - offsets[0]) < 200  # ~20ms sleep + overhead


def test_find_filters_by_type_and_phone() -> None:
    tl = Timeline()
    bus = EventBus()
    rec = TimelineRecorder(bus, tl)
    rec.start()
    bus.emit(Event(type="call.state.confirmed", phone_id="a"))
    bus.emit(Event(type="call.state.confirmed", phone_id="b"))
    bus.emit(Event(type="call.state.disconnected", phone_id="a"))
    rec.stop()

    a_confirmed = tl.find("call.state.confirmed", phone_id="a")
    assert len(a_confirmed) == 1
    assert a_confirmed[0].phone_id == "a"
    assert tl.has_any("call.state.disconnected")
    assert not tl.has_any("call.state.disconnected", phone_id="b")


def test_entry_to_dict_is_json_ready() -> None:
    e = TimelineEntry(
        kind="event",
        ts=100.5,
        ts_offset_ms=500.12345,
        type="call.state.confirmed",
        phone_id="a",
        call_id=1,
        data={"k": "v"},
    )
    d = e.to_dict()
    assert d["kind"] == "event"
    assert d["ts_offset_ms"] == 500.12       # rounded to 2 decimals
    assert d["phone_id"] == "a"
    assert d["data"] == {"k": "v"}
