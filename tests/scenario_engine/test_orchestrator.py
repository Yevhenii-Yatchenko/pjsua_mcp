"""Integration-style tests for the orchestrator using mock pjsua managers.

We don't touch real pjsua here — we fake `CallManager` enough to observe which
actions fire and emit synthetic events to drive the scenario forward.
"""

from __future__ import annotations

import asyncio
import time

from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.orchestrator import Scenario, ScenarioRunner


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
    """Minimal PhoneRegistry stub — records send_message and answers
    get_registration_info from a writable dict (pre-set by tests that
    want to drive the synthetic-reg-success replay)."""

    def __init__(self, reg_state: dict[str, dict] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._reg_state: dict[str, dict] = reg_state or {}

    def send_message(self, dest_uri, body, phone_id=None, content_type="text/plain"):
        self.calls.append(("send_message", {
            "phone_id": phone_id, "dest_uri": dest_uri,
            "body": body, "content_type": content_type,
        }))

    def get_registration_info(self, phone_id):
        return dict(self._reg_state.get(phone_id, {"is_registered": False}))


class MockEngine:
    """Minimal SipEngine stub. Records set_codecs and counts thread-registration
    calls (which ActionExecutor._run_pj makes before every dispatch, see P0 fix
    commit 5292cf4)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.thread_registrations: int = 0

    def set_codecs(self, codecs):
        self.calls.append(("set_codecs", {"codecs": list(codecs)}))
        return [{"codec": c, "priority": 250 - i} for i, c in enumerate(codecs)]

    def register_current_thread(self) -> None:
        self.thread_registrations += 1


def _run(coro):
    """Helper: run an async coroutine without pytest-asyncio."""
    return asyncio.run(coro)


def _spin_runner(call_mgr, registry, engine, bus, loop) -> ScenarioRunner:
    """Build a ScenarioRunner from already-made parts."""
    return ScenarioRunner(
        bus=bus,
        call_manager=call_mgr,
        registry=registry,
        loop=loop,
        engine=engine,
    )


def test_inline_auto_answer_hook_dispatches_answer() -> None:
    """Inline hook on call.state.incoming dispatches `answer`."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_incoming_soon() -> None:
            await asyncio.sleep(0.1)
            bus.emit(Event(type="call.state.incoming", phone_id="b", call_id=42))

        scenario = Scenario(
            name="test-auto-answer",
            phones=["b"],
            hooks=[{
                "when": "call.state.incoming",
                "on_phone": "b",
                "once": True,
                "then": ["answer"],
            }],
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


def test_inline_send_dtmf_after_confirmed_with_delay() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_confirmed_soon() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.confirmed", phone_id="a", call_id=7))

        scenario = Scenario(
            name="test-dtmf",
            phones=["a"],
            hooks=[{
                "when": "call.state.confirmed",
                "on_phone": "a",
                "once": True,
                "then": [{"wait": "50ms"}, {"send_dtmf": "1234"}],
            }],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)
        scenario = Scenario(
            name="test-timeout",
            phones=["a"],
            stop_on=[{"phone_id": "a", "event": "user.never_fires"}],
            timeout_ms=200,
        )
        t0 = time.monotonic()
        result = await runner.run(scenario)
        elapsed = time.monotonic() - t0
        assert result.status == "timeout"
        assert 0.15 < elapsed < 0.50, f"unexpected elapsed: {elapsed}"

    _run(inner())


def test_make_call_initial_action() -> None:
    """initial_actions can place an outbound call."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)
        scenario = Scenario(
            name="test-make-call",
            phones=["a"],
            initial_actions=[
                {"action": "make_call", "phone_id": "a", "dest_uri": "sip:1002@asterisk"},
            ],
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


def test_auto_validation_rejects_scenario_before_running() -> None:
    """Auto-validation fails fast — no wait until timeout."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="bad-action-fast-fail",
            phones=["a"],
            initial_actions=[{"frobnicate": "xyz"}],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=5000,
        )
        t0 = time.monotonic()
        result = await runner.run(scenario)
        elapsed = time.monotonic() - t0
        assert result.status == "error"
        assert result.reason == "pre-flight validation failed"
        assert elapsed < 0.30, f"validation should fail fast; got {elapsed}s"
        assert cm.calls == []

    _run(inner())


def test_skip_validation_bypasses_preflight() -> None:
    """skip_validation=True lets engine see the bad action at runtime."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="skip-validation",
            phones=["a"],
            initial_actions=[{"frobnicate": "xyz"}],
            stop_on=[{"phone_id": "a", "event": "user.never_fires"}],
            timeout_ms=200,
        )
        result = await runner.run(scenario, skip_validation=True)
        assert result.status in ("timeout", "error")

    _run(inner())


def test_attended_transfer_action_dispatches_to_call_manager() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="attended-xfer-smoke",
            phones=["a"],
            initial_actions=[
                {"action": "attended_transfer", "phone_id": "a", "call_id": 1, "dest_call_id": 2}
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=200,
        )
        result = await runner.run(scenario)
        xfer = [c for c in cm.calls if c[0] == "attended_transfer"]
        assert xfer, f"attended_transfer not invoked; got: {cm.calls}"
        assert xfer[0][1]["call_id"] == 1
        assert xfer[0][1]["dest_call_id"] == 2
        assert result.status == "timeout"

    _run(inner())


def test_conference_action() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

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
        runner = _spin_runner(cm, reg_mock, MockEngine(), bus, loop)

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
    """Scenario can define hooks inline without a pattern wrapper."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_confirmed_soon() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.confirmed", phone_id="a", call_id=1))

        scenario = Scenario(
            name="inline-hook-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_wrong_then_right() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.disconnected", phone_id="a", call_id=1))
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.disconnected", phone_id="a", call_id=2))

        scenario = Scenario(
            name="stop-on-match",
            phones=["a"],
            stop_on=[{"phone_id": "a", "call_id": 2, "event": "call.state.disconnected"}],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_wrong_then_right())
        t0 = time.monotonic()
        result = await runner.run(scenario)
        await task
        elapsed = time.monotonic() - t0
        assert result.status == "ok"
        assert 0.08 < elapsed < 0.30, f"unexpected elapsed: {elapsed}"

    _run(inner())


def test_stop_on_match_predicate_last_status() -> None:
    """stop_on.match on event.data field (e.g. status class 4xx)."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_ok_then_busy() -> None:
            await asyncio.sleep(0.03)
            bus.emit(Event(
                type="call.state.disconnected", phone_id="a", call_id=1,
                data={"last_status": 200},
            ))
            await asyncio.sleep(0.03)
            bus.emit(Event(
                type="call.state.disconnected", phone_id="a", call_id=2,
                data={"last_status": 486},
            ))

        scenario = Scenario(
            name="stop-on-bad-status",
            phones=["a"],
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

        def fake_active(phone_id=None):
            return [
                {"call_id": 10, "state": "CONFIRMED", "phone_id": "a"},
                {"call_id": 20, "state": "CONFIRMED", "phone_id": "a"},
            ]
        cm.get_active_calls = fake_active  # type: ignore[assignment]

        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)
        scenario = Scenario(
            name="conf-auto",
            phones=["a"],
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


def test_synthetic_reg_success_emitted_for_preregistered_phone() -> None:
    """Phones already registered before run_scenario starts get a synthetic
    reg.success event so wait-for-registration / similar barriers don't time out."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        reg = MockRegistry(reg_state={
            "a": {"is_registered": True, "status_code": 200, "reason": "OK", "expires": 600},
            "b": {"is_registered": False},
        })
        runner = _spin_runner(cm, reg, MockEngine(), bus, loop)

        scenario = Scenario(
            name="synth-reg-replay",
            phones=["a", "b"],
            stop_on=[{"phone_id": "a", "event": "reg.success"}],
            timeout_ms=500,
        )
        result = await runner.run(scenario)
        assert result.status == "ok", f"reason={result.reason}"
        reg_events = [
            e for e in result.timeline
            if e["kind"] == "event" and e["type"] == "reg.success"
        ]
        assert reg_events, f"no reg.success in timeline: {result.timeline}"
        assert reg_events[0]["phone_id"] == "a"
        assert reg_events[0]["data"].get("synthetic") is True
        assert all(e["phone_id"] != "b" for e in reg_events)

    _run(inner())


def test_scenario_started_fires_after_hooks_are_armed() -> None:
    """Coordinator-hook idiom: `when: scenario.started` must catch its own
    trigger. Earlier the bus event was emitted before scenario.hooks were
    armed, so the hook silently missed it; this regresses that fix."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="coordinator-hook-smoke",
            phones=["a"],
            hooks=[
                {
                    "when": "scenario.started",
                    "once": True,
                    "then": [
                        {"emit": {"name": "coordinator_ran"}},
                    ],
                },
            ],
            stop_on=[{"event": "user.coordinator_ran"}],
            timeout_ms=500,
        )
        result = await runner.run(scenario)
        assert result.status == "ok", f"reason={result.reason}"
        assert any(
            e["type"] == "user.coordinator_ran" for e in result.timeline
        ), "coordinator hook never fired its emit"

    _run(inner())


def test_synthetic_reg_replay_skipped_when_registry_is_none() -> None:
    """ScenarioRunner with `registry=None` (engine-only tests) must not crash."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = ScenarioRunner(
            bus=bus, call_manager=cm, registry=None, loop=loop,
        )
        scenario = Scenario(
            name="no-registry",
            phones=["a"],
            stop_on=[{"phone_id": "a", "event": "user.never_fires"}],
            timeout_ms=100,
        )
        result = await runner.run(scenario)
        assert result.status == "timeout"

    _run(inner())


def test_set_codecs_action_with_and_without_engine() -> None:
    async def with_engine() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        eng = MockEngine()
        runner = _spin_runner(cm, MockRegistry(), eng, bus, loop)

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
        runner = _spin_runner(cm, MockRegistry(), None, bus, loop)

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
        assert any("set_codecs" in str(e) or "SipEngine" in str(e) for e in result.errors)

    _run(with_engine())
    _run(without_engine())


# ============================================================================
# Block 4 Group A — orchestrator-level invariants
# ============================================================================


def test_hook_chain_failure_does_not_affect_other_hooks() -> None:
    """Hook A's action raises → hook A's remaining actions are skipped,
    but hook B (armed on a different event) still fires when its event
    arrives."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_sequence() -> None:
            await asyncio.sleep(0.05)
            # Trigger hook A — it will raise mid-chain
            bus.emit(Event(type="user.go_a", phone_id="a", call_id=1))
            await asyncio.sleep(0.05)
            # Trigger hook B — must still fire despite A's failure
            bus.emit(Event(type="user.go_b", phone_id="a", call_id=2))

        scenario = Scenario(
            name="hook-isolation",
            phones=["a"],
            hooks=[
                {
                    "when": "user.go_a",
                    "once": True,
                    "then": [
                        # send_dtmf with no `digits` raises ActionError
                        {"action": "send_dtmf"},
                        {"action": "hangup"},   # MUST be skipped
                    ],
                },
                {
                    "when": "user.go_b",
                    "once": True,
                    "then": [{"action": "hangup", "phone_id": "a", "call_id": 2}],
                },
            ],
            stop_on=[{"event": "call.state.disconnected", "call_id": 2}],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_sequence())
        result = await runner.run(scenario)
        await task
        # Hook B's hangup ran (call_id 2)
        hb = [c for c in cm.calls if c[0] == "hangup" and c[1]["call_id"] == 2]
        assert hb, f"hook B was not isolated from hook A's failure: {cm.calls}"
        # Hook A's hangup did NOT run (call_id 1)
        ha = [c for c in cm.calls if c[0] == "hangup" and c[1]["call_id"] == 1]
        assert not ha, "hook A's chain should have aborted before hangup"

    _run(inner())


def test_when_as_list_fires_for_either_event() -> None:
    """Hook with when: [reg.success, reg.failed] fires on EITHER event type."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_failed() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="reg.failed", phone_id="a"))

        scenario = Scenario(
            name="when-list",
            phones=["a"],
            hooks=[{
                "when": ["reg.success", "reg.failed"],
                "on_phone": "a",
                "once": True,
                "then": [{"action": "emit", "name": "either_reg_ev_seen"}],
            }],
            stop_on=[{"event": "user.either_reg_ev_seen"}],
            timeout_ms=1000,
        )
        task = asyncio.create_task(emit_failed())
        result = await runner.run(scenario)
        await task
        assert result.status == "ok", f"hook didn't fire on reg.failed: {result.reason}"

    _run(inner())


def test_once_false_hook_fires_repeatedly() -> None:
    """once: false → hook stays armed across multiple matching events."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_three_dtmf() -> None:
            for digit in ("1", "2", "3"):
                await asyncio.sleep(0.02)
                bus.emit(Event(type="dtmf.in", phone_id="a", call_id=1,
                               data={"digit": digit}))
            await asyncio.sleep(0.05)
            bus.emit(Event(type="user.done"))

        scenario = Scenario(
            name="once-false",
            phones=["a"],
            hooks=[{
                "when": "dtmf.in",
                "on_phone": "a",
                "once": False,
                "then": [{"action": "emit", "name": "got_digit"}],
            }],
            stop_on=[{"event": "user.done"}],
            timeout_ms=1500,
        )
        task = asyncio.create_task(emit_three_dtmf())
        result = await runner.run(scenario)
        await task
        emitted = [
            e for e in result.timeline
            if e["kind"] == "event" and e["type"] == "user.got_digit"
        ]
        assert len(emitted) == 3, (
            f"once: false hook fired {len(emitted)} times, expected 3"
        )

    _run(inner())


def test_empty_stop_on_runs_full_duration_with_ok_status() -> None:
    """No stop_on entries → scenario runs full timeout_ms and returns ok."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="full-duration",
            phones=["a"],
            stop_on=[],
            timeout_ms=200,
        )
        t0 = time.monotonic()
        result = await runner.run(scenario)
        elapsed = time.monotonic() - t0
        assert result.status == "ok"
        assert "full duration" in result.reason or "no stop_on" in result.reason
        # Should run roughly the timeout
        assert 0.18 < elapsed < 0.40

    _run(inner())


def test_multiple_stop_on_entries_either_can_trigger() -> None:
    """stop_on with two specs: first matching event ends the scenario."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_second_stop() -> None:
            await asyncio.sleep(0.05)
            # First stop_on (call.state.disconnected) doesn't fire — emit
            # the second stop_on event instead
            bus.emit(Event(type="user.also_stop"))

        scenario = Scenario(
            name="multi-stop",
            phones=["a"],
            stop_on=[
                {"phone_id": "a", "event": "call.state.disconnected"},
                {"event": "user.also_stop"},
            ],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_second_stop())
        result = await runner.run(scenario)
        await task
        assert result.status == "ok"
        # Stopped quickly because user.also_stop matched
        assert result.elapsed_ms < 500

    _run(inner())


def test_scenario_stopped_event_carries_status_and_reason() -> None:
    """scenario.stopped bus event must include status + reason in data."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        # Subscribe a probe before run
        captured: list[Event] = []
        bus.subscribe("scenario.stopped", lambda e: captured.append(e))

        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)
        scenario = Scenario(
            name="stopped-payload",
            phones=["a"],
            stop_on=[{"event": "user.never"}],
            timeout_ms=100,
        )
        result = await runner.run(scenario)
        # Give the bus dispatch a moment
        await asyncio.sleep(0.02)
        assert captured, "scenario.stopped never reached the bus"
        assert captured[0].data.get("status") == "timeout"
        assert "timeout" in captured[0].data.get("reason", "").lower()

    _run(inner())


# ============================================================================
# Block 4 Group B — artifact collection (proposal-04)
# ============================================================================


def test_run_attaches_artifacts_for_files_created_during_run(tmp_path) -> None:
    """ScenarioRunner sweeps recording/capture roots after stop and adds the
    result to ScenarioResult.artifacts."""
    import os

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        rec_root = tmp_path / "recordings"
        cap_root = tmp_path / "captures"
        rec_root.mkdir()
        cap_root.mkdir()

        # Pre-existing (must be ignored — older mtime than started_at).
        old_wav = rec_root / "a" / "old.wav"
        old_wav.parent.mkdir()
        old_wav.write_bytes(b"")
        os.utime(old_wav, (1.0, 1.0))

        runner = ScenarioRunner(
            bus=bus,
            call_manager=cm,
            registry=MockRegistry(),
            loop=loop,
            engine=MockEngine(),
            recordings_root=rec_root,
            captures_root=cap_root,
            host_recordings_root="/host/rec",
            host_captures_root="/host/cap",
        )

        async def emit_done_then_drop_files() -> None:
            await asyncio.sleep(0.05)
            # File freshly created during scenario — must be picked.
            new_wav = rec_root / "a" / "call_0_now.wav"
            new_wav.write_bytes(b"")
            new_pcap = cap_root / "a" / "call_0_now.pcap"
            new_pcap.parent.mkdir(parents=True, exist_ok=True)
            new_pcap.write_bytes(b"")
            await asyncio.sleep(0.02)
            bus.emit(Event(type="user.done"))

        scenario = Scenario(
            name="artifacts-smoke",
            phones=["a", "b"],
            stop_on=[{"event": "user.done"}],
            timeout_ms=2000,
        )
        task = asyncio.create_task(emit_done_then_drop_files())
        result = await runner.run(scenario)
        await task
        assert result.status == "ok"

        d = result.to_dict()
        assert "artifacts" in d
        a = d["artifacts"]["a"]
        assert a is not None
        assert a["recording"].endswith("/a/call_0_now.wav")
        assert a["pcap"].endswith("/a/call_0_now.pcap")
        assert a["host_recording"] == "/host/rec/a/call_0_now.wav"
        assert a["host_pcap"] == "/host/cap/a/call_0_now.pcap"
        # `b` was in phones but produced nothing → null.
        assert d["artifacts"]["b"] is None

    _run(inner())


def test_run_artifacts_default_empty_when_roots_unset() -> None:
    """When recordings/captures roots are not passed (older callers), the
    result still has an `artifacts` key — empty dict, never missing."""

    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = ScenarioRunner(
            bus=bus, call_manager=cm, registry=MockRegistry(), loop=loop,
        )
        scenario = Scenario(
            name="no-roots",
            phones=["a"],
            stop_on=[{"event": "user.done"}],
            timeout_ms=500,
        )

        async def emit_done() -> None:
            await asyncio.sleep(0.02)
            bus.emit(Event(type="user.done"))

        task = asyncio.create_task(emit_done())
        result = await runner.run(scenario)
        await task
        d = result.to_dict()
        assert d.get("artifacts") == {}

    _run(inner())
