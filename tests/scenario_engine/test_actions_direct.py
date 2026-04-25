"""Direct coverage for actions not exercised via patterns.

Each test dispatches the action via the orchestrator's initial_actions and
verifies the MockCallManager received the correct call or — for flow-control
actions (wait_until, emit, checkpoint, log) — that the timeline has the
expected entry.
"""

from __future__ import annotations

import asyncio
import time

from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.orchestrator import Scenario, ScenarioRunner
from src.scenario_engine.pattern_loader import PatternRegistry

from tests.scenario_engine.test_orchestrator import (
    MockCallManager,
    MockEngine,
    MockRegistry,
    _spin_runner,
    PATTERNS_DIR,
)


def _run(coro):
    return asyncio.run(coro)


def test_hangup_dispatches_to_call_manager() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hangup-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[{"action": "hangup", "phone_id": "a", "call_id": 5}],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        hu = [c for c in cm.calls if c[0] == "hangup"]
        assert hu and hu[0][1]["phone_id"] == "a" and hu[0][1]["call_id"] == 5

    _run(inner())


def test_reject_dispatches_with_status_code() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="reject-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "reject", "phone_id": "a", "call_id": 3, "code": 603},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        rj = [c for c in cm.calls if c[0] == "reject_call"]
        assert rj and rj[0][1]["status_code"] == 603

    _run(inner())


def test_hold_and_unhold_dispatch() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hold-unhold-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "hold", "phone_id": "a", "call_id": 1},
                {"action": "unhold", "phone_id": "a", "call_id": 1},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        names = [c[0] for c in cm.calls]
        assert "hold" in names
        assert "unhold" in names

    _run(inner())


def test_blind_transfer_dispatch() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="blind-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "blind_transfer", "phone_id": "a", "call_id": 2,
                 "to": "sip:6003@asterisk"},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        bt = [c for c in cm.calls if c[0] == "blind_transfer"]
        assert bt and bt[0][1]["dest_uri"] == "sip:6003@asterisk"

    _run(inner())


def test_wait_duration_parses_and_sleeps() -> None:
    """`wait` action supports int ms, '500ms', '2s' — must actually sleep."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="wait-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[{"wait": "150ms"}, {"action": "hangup", "phone_id": "a", "call_id": 1}],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=500,
        )
        t0 = time.monotonic()
        await runner.run(scenario)
        elapsed = time.monotonic() - t0
        # 150ms wait + small overhead; must be at least 100ms
        assert elapsed >= 0.10, f"wait did not sleep; elapsed={elapsed}"

    _run(inner())


def test_wait_until_blocks_on_event() -> None:
    """wait_until blocks hook execution until event arrives."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_target_later() -> None:
            await asyncio.sleep(0.1)
            bus.emit(Event(type="user.my_signal"))

        scenario = Scenario(
            name="wait-until-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "wait_until", "event": "user.my_signal", "timeout_ms": 2000},
                {"action": "hangup", "phone_id": "a", "call_id": 9},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=1500,
        )
        task = asyncio.create_task(emit_target_later())
        await runner.run(scenario)
        await task
        hu = [c for c in cm.calls if c[0] == "hangup"]
        assert hu, "hangup should have run AFTER user.my_signal fired"

    _run(inner())


def test_emit_action_pushes_user_event() -> None:
    """emit triggers stop_on via user.<name> event."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="emit-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "emit", "name": "checkpoint_reached"},
            ],
            stop_on=[{"event": "user.checkpoint_reached"}],
            timeout_ms=500,
        )
        t0 = time.monotonic()
        result = await runner.run(scenario)
        elapsed = time.monotonic() - t0
        assert result.status == "ok"
        # Should stop almost immediately — emit fires synchronously
        assert elapsed < 0.30, f"emit+stop should be fast; elapsed={elapsed}"

    _run(inner())


def test_hangup_all_dispatches() -> None:
    """hangup_all should call CallManager.hangup_all (optionally scoped to phone)."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hangup-all-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[{"action": "hangup_all", "phone_id": "a"}],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        calls = [c for c in cm.calls if c[0] == "hangup_all"]
        assert calls and calls[0][1]["phone_id"] == "a"

    _run(inner())


def test_hangup_all_without_phone_id_is_global() -> None:
    """Omitting phone_id triggers a global hangup_all."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hangup-all-global",
            phones=["a"],
            patterns=[],
            initial_actions=[{"action": "hangup_all"}],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        calls = [c for c in cm.calls if c[0] == "hangup_all"]
        assert calls and calls[0][1]["phone_id"] is None

    _run(inner())


def test_send_dtmf_action_emits_dtmf_out_event() -> None:
    """ActionExecutor._a_send_dtmf must emit `dtmf.out` after dispatch — required
    for symmetry with `dtmf.in` (which fires from pjsua's onDtmfDigit callback)."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="dtmf-out-emission",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "send_dtmf", "phone_id": "a", "call_id": 7, "digits": "42"},
            ],
            stop_on=[{"phone_id": "a", "event": "dtmf.out"}],
            timeout_ms=500,
        )
        t0 = time.monotonic()
        result = await runner.run(scenario)
        elapsed = time.monotonic() - t0
        assert result.status == "ok", f"reason={result.reason}"
        # Should resolve fast: emit happens immediately after the dispatch.
        assert elapsed < 0.30, f"dtmf.out emit was slow; elapsed={elapsed}"
        outs = [e for e in result.timeline if e["type"] == "dtmf.out" and e["kind"] == "event"]
        assert outs, f"no dtmf.out in timeline: {result.timeline}"
        assert outs[0]["data"].get("digits") == "42"
        assert outs[0]["data"].get("method") == "rfc2833"
        assert outs[0]["call_id"] == 7
        assert outs[0]["phone_id"] == "a"

    _run(inner())


def test_checkpoint_and_log_land_in_timeline() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="meta-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "checkpoint", "label": "start"},
                {"action": "log", "message": "hello"},
                {"action": "checkpoint", "label": "end"},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        result = await runner.run(scenario)
        meta_types = [e["type"] for e in result.timeline if e["kind"] == "meta"]
        assert "checkpoint" in meta_types
        assert "log" in meta_types
        labels = [e["data"].get("label") for e in result.timeline
                  if e["type"] == "checkpoint"]
        assert "start" in labels
        assert "end" in labels

    _run(inner())
