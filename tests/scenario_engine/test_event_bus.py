"""Unit tests for EventBus (pub/sub, wildcards, once, threading-safety)."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from src.scenario_engine.event_bus import Event, EventBus


def test_emit_and_subscribe_exact_match() -> None:
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("call.state.confirmed", received.append)
    bus.emit(Event(type="call.state.confirmed", phone_id="a"))
    assert len(received) == 1
    assert received[0].type == "call.state.confirmed"


def test_subscriber_only_receives_matching_types() -> None:
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("call.state.confirmed", received.append)
    bus.emit(Event(type="call.state.incoming"))
    bus.emit(Event(type="call.state.confirmed"))
    bus.emit(Event(type="reg.success"))
    assert len(received) == 1
    assert received[0].type == "call.state.confirmed"


def test_prefix_wildcard() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("call.state.*", lambda e: received.append(e.type))
    bus.emit(Event(type="call.state.incoming"))
    bus.emit(Event(type="call.state.confirmed"))
    bus.emit(Event(type="call.media.state"))
    assert received == ["call.state.incoming", "call.state.confirmed"]


def test_full_wildcard() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("*", lambda e: received.append(e.type))
    bus.emit(Event(type="call.state.confirmed"))
    bus.emit(Event(type="reg.success"))
    bus.emit(Event(type="dtmf.in"))
    assert received == ["call.state.confirmed", "reg.success", "dtmf.in"]


def test_once_removes_after_first_fire() -> None:
    bus = EventBus()
    received: list[str] = []
    bus.subscribe("x.y", lambda e: received.append(e.type), once=True)
    bus.emit(Event(type="x.y"))
    bus.emit(Event(type="x.y"))
    bus.emit(Event(type="x.y"))
    assert received == ["x.y"]


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    received: list[Event] = []
    sid = bus.subscribe("x.y", received.append)
    bus.emit(Event(type="x.y"))
    bus.unsubscribe(sid)
    bus.emit(Event(type="x.y"))
    assert len(received) == 1


def test_subscriber_exception_does_not_break_others() -> None:
    bus = EventBus()
    received: list[str] = []

    def bad(_: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe("x.y", bad)
    bus.subscribe("x.y", lambda e: received.append(e.type))
    bus.emit(Event(type="x.y"))
    assert received == ["x.y"]


def test_emit_from_multiple_threads_is_safe() -> None:
    bus = EventBus()
    counter: list[int] = []
    lock = threading.Lock()

    def sub(_: Event) -> None:
        with lock:
            counter.append(1)

    bus.subscribe("race.check", sub)

    def worker() -> None:
        for _ in range(200):
            bus.emit(Event(type="race.check"))

    ts = [threading.Thread(target=worker) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(counter) == 1000


def test_wait_for_matches_event() -> None:
    async def run() -> None:
        bus = EventBus(loop=asyncio.get_running_loop())

        async def emit_later() -> None:
            await asyncio.sleep(0.02)
            bus.emit(Event(type="target", phone_id="a"))

        task = asyncio.create_task(emit_later())
        ev = await bus.wait_for("target", timeout=1.0)
        await task
        assert ev.type == "target"
        assert ev.phone_id == "a"

    asyncio.run(run())


def test_wait_for_raises_on_timeout() -> None:
    async def run() -> None:
        bus = EventBus(loop=asyncio.get_running_loop())
        with pytest.raises(asyncio.TimeoutError):
            await bus.wait_for("never-fires", timeout=0.05)

    asyncio.run(run())
