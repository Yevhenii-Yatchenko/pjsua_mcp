"""Unit tests for HookRuntime match engine."""

from __future__ import annotations

import asyncio

import pytest

from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.hook_runtime import (
    Hook,
    HookRuntime,
    _event_matches_predicates,
    _hook_matches,
    _value_matches,
)


def test_value_matches_exact() -> None:
    assert _value_matches(42, 42)
    assert not _value_matches(42, 43)


def test_value_matches_list_membership() -> None:
    assert _value_matches([1, 2, 3], 2)
    assert not _value_matches([1, 2, 3], 4)


def test_value_matches_regex() -> None:
    assert _value_matches("~Q\\.850", "Q.850;cause=16")
    assert not _value_matches("~Q\\.850", "something else")


def test_value_matches_status_class() -> None:
    assert _value_matches("4xx", 486)
    assert _value_matches("4xx", 400)
    assert not _value_matches("4xx", 500)
    assert _value_matches("5xx", 503)


def test_event_matches_predicates_with_data() -> None:
    ev = Event(type="x", data={"status_code": 200, "method": "INVITE"})
    assert _event_matches_predicates(ev, {"status_code": 200})
    assert _event_matches_predicates(ev, {"status_code": 200, "method": "INVITE"})
    assert not _event_matches_predicates(ev, {"status_code": 486})


def test_hook_matches_on_phone() -> None:
    hook = Hook(
        hook_id="h1",
        when=["x"],
        on_phone="a",
        match={},
        then=[],
        once=True,
        pattern_name="t",
    )
    assert _hook_matches(hook, Event(type="x", phone_id="a"))
    assert not _hook_matches(hook, Event(type="x", phone_id="b"))


def test_hook_matches_with_no_phone_filter() -> None:
    hook = Hook(
        hook_id="h1",
        when=["x"],
        on_phone=None,
        match={},
        then=[],
        once=True,
        pattern_name="t",
    )
    assert _hook_matches(hook, Event(type="x", phone_id="a"))
    assert _hook_matches(hook, Event(type="x", phone_id=None))


def test_arm_and_fire_schedules_actions() -> None:
    """Full wiring: arm a hook, emit matching event, verify action_executor runs."""

    async def run() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        fired: list[tuple] = []

        async def fake_executor(actions, hook, event):
            fired.append((hook.pattern_name, list(actions), event.type))

        runtime = HookRuntime(bus, fake_executor, loop)
        runtime.arm(
            {
                "when": "call.state.confirmed",
                "on_phone": "a",
                "then": [{"send_dtmf": "1"}],
            },
            pattern_name="test-pat",
        )
        bus.emit(Event(type="call.state.confirmed", phone_id="a"))
        await asyncio.sleep(0.05)
        assert len(fired) == 1
        assert fired[0][0] == "test-pat"
        assert fired[0][1] == [{"send_dtmf": "1"}]

    asyncio.run(run())


def test_hook_respects_phone_filter() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        fired: list[str] = []

        async def exec_fn(actions, hook, event):
            fired.append(event.phone_id or "?")

        runtime = HookRuntime(bus, exec_fn, loop)
        runtime.arm(
            {"when": "x.y", "on_phone": "a", "then": ["answer"]},
            pattern_name="p",
        )
        bus.emit(Event(type="x.y", phone_id="b"))
        bus.emit(Event(type="x.y", phone_id="a"))
        await asyncio.sleep(0.05)
        assert fired == ["a"]

    asyncio.run(run())


def test_once_true_removes_after_fire() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        fired: list[int] = []

        async def exec_fn(actions, hook, event):
            fired.append(1)

        runtime = HookRuntime(bus, exec_fn, loop)
        runtime.arm(
            {"when": "x.y", "on_phone": "a", "once": True, "then": ["answer"]},
            pattern_name="p",
        )
        bus.emit(Event(type="x.y", phone_id="a"))
        bus.emit(Event(type="x.y", phone_id="a"))
        bus.emit(Event(type="x.y", phone_id="a"))
        await asyncio.sleep(0.05)
        assert len(fired) == 1

    asyncio.run(run())


def test_remove_all_detaches_all_hooks() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        fired: list[int] = []

        async def exec_fn(actions, hook, event):
            fired.append(1)

        runtime = HookRuntime(bus, exec_fn, loop)
        runtime.arm({"when": "x", "on_phone": "a", "then": ["answer"]}, pattern_name="p1")
        runtime.arm({"when": "x", "on_phone": "a", "then": ["hangup"]}, pattern_name="p2")
        runtime.remove_all()
        bus.emit(Event(type="x", phone_id="a"))
        await asyncio.sleep(0.05)
        assert fired == []

    asyncio.run(run())
