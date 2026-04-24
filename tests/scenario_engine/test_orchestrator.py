"""Integration-style tests for the orchestrator using mock pjsua managers.

We don't touch real pjsua here — we fake `CallManager` enough to observe which
actions fire and emit synthetic events to drive the scenario forward.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.orchestrator import Scenario, ScenarioRunner
from src.scenario_engine.pattern_loader import PatternRegistry
from tests.scenario_engine.test_pattern_loader import PATTERNS_DIR


class MockCallManager:
    """Records calls to pjsua-facing methods and emits synthetic events."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.calls: list[tuple[str, dict]] = []
        self._next_call_id = 100

    def make_call(self, dest_uri, phone_id=None, headers=None):
        cid = self._next_call_id
        self._next_call_id += 1
        self.calls.append(("make_call", {"dest_uri": dest_uri, "phone_id": phone_id, "call_id": cid}))
        # Synthesise state transitions asynchronously from a different "thread"
        self.bus.emit(Event(type="call.state.calling", phone_id=phone_id, call_id=cid))
        self.bus.emit(Event(type="call.state.confirmed", phone_id=phone_id, call_id=cid))
        return {"phone_id": phone_id, "call_id": cid, "state": "CONFIRMED"}

    def answer_call(self, phone_id=None, call_id=None, status_code=200):
        self.calls.append(("answer_call", {"phone_id": phone_id, "call_id": call_id, "status_code": status_code}))
        self.bus.emit(Event(type="call.state.confirmed", phone_id=phone_id, call_id=call_id))
        return {"phone_id": phone_id, "call_id": call_id, "state": "CONFIRMED"}

    def hangup(self, phone_id=None, call_id=None):
        self.calls.append(("hangup", {"phone_id": phone_id, "call_id": call_id}))
        self.bus.emit(Event(type="call.state.disconnected", phone_id=phone_id, call_id=call_id))

    def reject_call(self, phone_id=None, call_id=None, status_code=486):
        self.calls.append(("reject_call", {"phone_id": phone_id, "call_id": call_id, "status_code": status_code}))
        self.bus.emit(Event(type="call.state.disconnected", phone_id=phone_id, call_id=call_id, data={"last_status": status_code}))

    def send_dtmf(self, call_id, digits, phone_id=None):
        self.calls.append(("send_dtmf", {"phone_id": phone_id, "call_id": call_id, "digits": digits}))
        self.bus.emit(Event(type="dtmf.out", phone_id=phone_id, call_id=call_id, data={"digit": digits}))

    def hold(self, call_id, phone_id=None):
        self.calls.append(("hold", {"phone_id": phone_id, "call_id": call_id}))

    def unhold(self, call_id, phone_id=None):
        self.calls.append(("unhold", {"phone_id": phone_id, "call_id": call_id}))

    def blind_transfer(self, dest_uri, phone_id=None, call_id=None):
        self.calls.append(("blind_transfer", {"phone_id": phone_id, "call_id": call_id, "dest_uri": dest_uri}))

    def attended_transfer(self, phone_id=None, call_id=None, dest_call_id=None):
        self.calls.append(("attended_transfer", {"phone_id": phone_id, "call_id": call_id, "dest_call_id": dest_call_id}))
        return {"phone_id": phone_id, "transferred": True}

    def conference(self, call_ids, phone_id=None):
        self.calls.append(("conference", {"phone_id": phone_id, "call_ids": list(call_ids)}))
        return {"phone_id": phone_id, "call_ids": list(call_ids), "participants": len(call_ids)}

    def play_audio(self, file_path, phone_id=None, call_id=None, loop=False):
        self.calls.append(("play_audio", {"phone_id": phone_id, "call_id": call_id, "file_path": file_path, "loop": loop}))
        return {"phone_id": phone_id, "playing_file": file_path}

    def stop_audio(self, phone_id=None, call_id=None):
        self.calls.append(("stop_audio", {"phone_id": phone_id, "call_id": call_id}))

    def hangup_all(self, phone_id=None):
        self.calls.append(("hangup_all", {"phone_id": phone_id}))


class MockRegistry:
    """Minimal PhoneRegistry stub that just records send_message calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def send_message(self, dest_uri, body, phone_id=None, content_type="text/plain"):
        self.calls.append(("send_message", {
            "phone_id": phone_id, "dest_uri": dest_uri,
            "body": body, "content_type": content_type,
        }))


class MockEngine:
    """Minimal SipEngine stub that records set_codecs calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def set_codecs(self, codecs):
        self.calls.append(("set_codecs", {"codecs": list(codecs)}))
        return [{"codec": c, "priority": 250 - i} for i, c in enumerate(codecs)]


def _run(coro):
    """Helper: run an async coroutine without pytest-asyncio."""
    return asyncio.run(coro)


def test_auto_answer_pattern_wires_up() -> None:
    """Symbolic: auto-answer hooks into call.state.incoming and dispatches `answer`."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        reg = PatternRegistry(PATTERNS_DIR)
        reg.scan()

        runner = ScenarioRunner(
            bus=bus, pattern_registry=reg, call_manager=cm, registry=None, loop=loop
        )

        async def emit_incoming_soon() -> None:
            await asyncio.sleep(0.1)
            bus.emit(Event(type="call.state.incoming", phone_id="b", call_id=42))

        scenario = Scenario(
            name="test-auto-answer",
            phones=["b"],
            patterns=[{"use": "auto-answer", "phone_id": "b", "delay_ms": 0}],
            stop_on=[{"phone_id": "b", "event": "call.state.confirmed"}],
            timeout_ms=3000,
        )
        task = asyncio.create_task(emit_incoming_soon())
        result = await runner.run(scenario)
        await task
        assert result.status == "ok", f"expected ok, got {result.status}: {result.reason}"
        answer_calls = [c for c in cm.calls if c[0] == "answer_call"]
        assert answer_calls, f"no answer_call recorded; got: {cm.calls}"
        assert answer_calls[0][1]["phone_id"] == "b"
        assert answer_calls[0][1]["call_id"] == 42

    _run(inner())


def test_send_dtmf_on_confirmed_fires_after_delay() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        reg = PatternRegistry(PATTERNS_DIR)
        reg.scan()

        runner = ScenarioRunner(
            bus=bus, pattern_registry=reg, call_manager=cm, registry=None, loop=loop
        )

        async def emit_confirmed_soon() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.confirmed", phone_id="a", call_id=7))

        scenario = Scenario(
            name="test-dtmf",
            phones=["a"],
            patterns=[{"use": "send-dtmf-on-confirmed", "phone_id": "a", "digits": "1234", "initial_delay_ms": 50}],
            stop_on=[{"phone_id": "a", "event": "dtmf.out"}],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_confirmed_soon())
        result = await runner.run(scenario)
        await task
        assert result.status == "ok"
        dtmf_calls = [c for c in cm.calls if c[0] == "send_dtmf"]
        assert dtmf_calls, f"no send_dtmf recorded; got: {cm.calls}"
        assert dtmf_calls[0][1]["digits"] == "1234"
        assert dtmf_calls[0][1]["call_id"] == 7

    _run(inner())


def test_timeout_when_no_stop_condition_matches() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        reg = PatternRegistry(PATTERNS_DIR)
        reg.scan()

        runner = ScenarioRunner(
            bus=bus, pattern_registry=reg, call_manager=cm, registry=None, loop=loop
        )
        scenario = Scenario(
            name="test-timeout",
            phones=["a"],
            patterns=[{"use": "auto-answer", "phone_id": "a"}],
            stop_on=[{"phone_id": "a", "event": "user.never_fires"}],
            timeout_ms=200,
        )
        t0 = time.monotonic()
        result = await runner.run(scenario)
        elapsed = time.monotonic() - t0
        assert result.status == "timeout"
        assert 0.15 < elapsed < 0.50, f"unexpected elapsed: {elapsed}"

    _run(inner())


def test_make_call_initial_action_from_pattern() -> None:
    """make-call-and-wait-confirmed has an initial_action — verify it runs."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        reg = PatternRegistry(PATTERNS_DIR)
        reg.scan()

        runner = ScenarioRunner(
            bus=bus, pattern_registry=reg, call_manager=cm, registry=None, loop=loop
        )
        scenario = Scenario(
            name="test-make-call",
            phones=["a"],
            patterns=[{
                "use": "make-call-and-wait-confirmed",
                "phone_id": "a",
                "dest_uri": "sip:1002@asterisk",
                "timeout_ms": 1000,
            }],
            stop_on=[{"phone_id": "a", "event": "call.state.confirmed"}],
            timeout_ms=3000,
        )
        result = await runner.run(scenario)
        assert result.status == "ok", f"result: {result.reason}"
        mk = [c for c in cm.calls if c[0] == "make_call"]
        assert mk, f"make_call was not invoked; got {cm.calls}"
        assert mk[0][1]["dest_uri"] == "sip:1002@asterisk"
        assert mk[0][1]["phone_id"] == "a"

    _run(inner())


def test_unknown_pattern_returns_error_without_crashing() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        reg = PatternRegistry(PATTERNS_DIR)
        reg.scan()

        runner = ScenarioRunner(
            bus=bus, pattern_registry=reg, call_manager=cm, registry=None, loop=loop
        )
        scenario = Scenario(
            name="test-bad-pattern",
            patterns=[{"use": "does-not-exist", "phone_id": "a"}],
            stop_on=[{"phone_id": "a", "event": "call.state.confirmed"}],
            timeout_ms=100,
        )
        result = await runner.run(scenario)
        assert result.status == "error"
        assert any("does-not-exist" in str(e) for e in result.errors)

    _run(inner())


def test_auto_validation_rejects_scenario_before_running() -> None:
    """Auto-validation fails fast — no wait until timeout."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        # Bad action name in initial_actions — validator should trip
        scenario = Scenario(
            name="bad-action-fast-fail",
            phones=["a"],
            patterns=[],
            initial_actions=[{"frobnicate": "xyz"}],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=5000,   # intentionally long: if validation works, we stop <100ms
        )
        t0 = time.monotonic()
        result = await runner.run(scenario)
        elapsed = time.monotonic() - t0
        assert result.status == "error"
        assert result.reason == "pre-flight validation failed"
        assert elapsed < 0.30, f"validation should fail fast; got {elapsed}s"
        # And the MockCallManager should never have been touched.
        assert cm.calls == []

    _run(inner())


def test_skip_validation_bypasses_preflight() -> None:
    """skip_validation=True lets engine see the bad action at runtime."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="skip-validation",
            phones=["a"],
            patterns=[],
            initial_actions=[{"frobnicate": "xyz"}],
            stop_on=[{"phone_id": "a", "event": "user.never_fires"}],
            timeout_ms=200,
        )
        result = await runner.run(scenario, skip_validation=True)
        # Without validation, the bogus action errors at runtime, and the
        # scenario then hits its timeout (because initial_actions raised
        # and stop_on never fires).
        assert result.status in ("timeout", "error")

    _run(inner())


# ---------------- new-action dispatch tests ----------------
# These exercise attended_transfer, conference, play_audio, stop_audio,
# send_message, set_codecs — the actions added beyond the initial MVP.

def _spin_runner(patterns_root, call_mgr, registry, engine, bus, loop):
    """Helper — build a ScenarioRunner from already-made parts."""
    from src.scenario_engine.orchestrator import ScenarioRunner
    reg = PatternRegistry(patterns_root)
    reg.scan()
    return ScenarioRunner(
        bus=bus,
        pattern_registry=reg,
        call_manager=call_mgr,
        registry=registry,
        loop=loop,
        engine=engine,
    )


def test_attended_transfer_action_dispatches_to_call_manager() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="attended-xfer-smoke",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "attended_transfer", "phone_id": "a", "call_id": 1, "dest_call_id": 2}
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=200,
        )
        result = await runner.run(scenario)
        # Expect timeout (no user.done ever fires) — but the action must have run.
        xfer = [c for c in cm.calls if c[0] == "attended_transfer"]
        assert xfer, f"attended_transfer not invoked; got: {cm.calls}"
        assert xfer[0][1]["call_id"] == 1
        assert xfer[0][1]["dest_call_id"] == 2
        assert result.status == "timeout"  # Sanity check the frame

    _run(inner())


def test_conference_action() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="conf-smoke",
            phones=["a"],
            initial_actions=[
                {"action": "conference", "phone_id": "a", "call_ids": [11, 22]}
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        conf = [c for c in cm.calls if c[0] == "conference"]
        assert conf
        assert conf[0][1]["call_ids"] == [11, 22]

    _run(inner())


def test_play_audio_and_stop_audio() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="audio-smoke",
            phones=["a"],
            initial_actions=[
                {"action": "play_audio", "phone_id": "a", "call_id": 5, "file": "/audio/test.wav"},
                {"action": "stop_audio", "phone_id": "a", "call_id": 5},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        names = [c[0] for c in cm.calls]
        assert "play_audio" in names
        assert "stop_audio" in names
        play = [c for c in cm.calls if c[0] == "play_audio"][0]
        assert play[1]["file_path"] == "/audio/test.wav"
        assert play[1]["loop"] is False

    _run(inner())


def test_send_message_action() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        reg_mock = MockRegistry()
        runner = _spin_runner(PATTERNS_DIR, cm, reg_mock, MockEngine(), bus, loop)

        scenario = Scenario(
            name="msg-smoke",
            phones=["a"],
            initial_actions=[
                {"action": "send_message", "phone_id": "a",
                 "to": "sip:bob@example", "body": "hi"}
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        msgs = [c for c in reg_mock.calls if c[0] == "send_message"]
        assert msgs, f"send_message not dispatched; got {reg_mock.calls}"
        assert msgs[0][1]["dest_uri"] == "sip:bob@example"
        assert msgs[0][1]["body"] == "hi"

    _run(inner())


def test_scenario_level_inline_hook_fires() -> None:
    """Scenario can define hooks inline, without a pattern wrapper."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_confirmed_soon() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.confirmed", phone_id="a", call_id=1))

        scenario = Scenario(
            name="inline-hook-smoke",
            phones=["a"],
            patterns=[],
            hooks=[
                {
                    "when": "call.state.confirmed",
                    "on_phone": "a",
                    "once": True,
                    "then": [{"send_dtmf": "9"}],
                },
            ],
            stop_on=[{"phone_id": "a", "event": "dtmf.out"}],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_confirmed_soon())
        result = await runner.run(scenario)
        await task
        assert result.status == "ok"
        dtmfs = [c for c in cm.calls if c[0] == "send_dtmf"]
        assert dtmfs and dtmfs[0][1]["digits"] == "9"

    _run(inner())


def test_stop_on_match_filters_by_call_id() -> None:
    """stop_on should ignore a disconnected event on the wrong call_id."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_wrong_then_right() -> None:
            await asyncio.sleep(0.05)
            # Wrong call_id first — should NOT trigger stop
            bus.emit(Event(type="call.state.disconnected", phone_id="a", call_id=1))
            await asyncio.sleep(0.05)
            # Right call_id — triggers stop
            bus.emit(Event(type="call.state.disconnected", phone_id="a", call_id=2))

        scenario = Scenario(
            name="stop-on-match",
            phones=["a"],
            patterns=[],
            stop_on=[{"phone_id": "a", "call_id": 2, "event": "call.state.disconnected"}],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_wrong_then_right())
        t0 = time.monotonic()
        result = await runner.run(scenario)
        await task
        elapsed = time.monotonic() - t0
        assert result.status == "ok"
        # Stop should happen ~0.1s (2nd event), not 0.05s (1st) and not on timeout.
        assert 0.08 < elapsed < 0.30, f"unexpected elapsed: {elapsed}"

    _run(inner())


def test_stop_on_match_predicate_last_status() -> None:
    """stop_on.match on event.data field (e.g. status class 4xx)."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_ok_then_busy() -> None:
            await asyncio.sleep(0.03)
            # Normal disconnect (status 200) — should NOT stop
            bus.emit(Event(
                type="call.state.disconnected", phone_id="a", call_id=1,
                data={"last_status": 200},
            ))
            await asyncio.sleep(0.03)
            # Busy disconnect (status 486) — stop
            bus.emit(Event(
                type="call.state.disconnected", phone_id="a", call_id=2,
                data={"last_status": 486},
            ))

        scenario = Scenario(
            name="stop-on-bad-status",
            phones=["a"],
            patterns=[],
            stop_on=[{
                "phone_id": "a",
                "event": "call.state.disconnected",
                "match": {"last_status": "4xx"},
            }],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_ok_then_busy())
        result = await runner.run(scenario)
        await task
        assert result.status == "ok"

    _run(inner())


def test_conference_auto_fills_call_ids() -> None:
    """`conference` with `call_ids: auto` reads active calls from CallManager."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)

        # Pretend phone a has two active calls already
        def fake_active(phone_id=None):
            return [
                {"call_id": 10, "state": "CONFIRMED", "phone_id": "a"},
                {"call_id": 20, "state": "CONFIRMED", "phone_id": "a"},
            ]
        cm.get_active_calls = fake_active  # type: ignore[assignment]

        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), MockEngine(), bus, loop)
        scenario = Scenario(
            name="conf-auto",
            phones=["a"],
            patterns=[],
            initial_actions=[
                {"action": "conference", "phone_id": "a", "call_ids": "auto"},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=200,
        )
        await runner.run(scenario)
        confs = [c for c in cm.calls if c[0] == "conference"]
        assert confs, f"conference not called; got {cm.calls}"
        assert confs[0][1]["call_ids"] == [10, 20]

    _run(inner())


def test_set_codecs_action_with_and_without_engine() -> None:
    async def with_engine() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        eng = MockEngine()
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), eng, bus, loop)

        scenario = Scenario(
            name="codecs-smoke",
            phones=["a"],
            initial_actions=[
                {"action": "set_codecs", "phone_id": "a", "codecs": ["G722", "PCMA"]}
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        assert eng.calls == [("set_codecs", {"codecs": ["G722", "PCMA"]})]

    async def without_engine() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(PATTERNS_DIR, cm, MockRegistry(), None, bus, loop)

        scenario = Scenario(
            name="codecs-no-engine",
            phones=["a"],
            initial_actions=[
                {"action": "set_codecs", "phone_id": "a", "codecs": ["G722"]}
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=100,
        )
        result = await runner.run(scenario)
        # set_codecs without engine should record an error but not crash the run.
        assert any("set_codecs" in str(e) or "SipEngine" in str(e) for e in result.errors)

    _run(with_engine())
    _run(without_engine())
