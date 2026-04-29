"""Direct coverage for actions not exercised via patterns.

Each test dispatches the action via the orchestrator's initial_actions and
verifies the MockCallManager received the correct call or — for flow-control
actions (wait_until, emit, checkpoint, log) — that the timeline has the
expected entry.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.orchestrator import Scenario

from tests.scenario_engine.test_orchestrator import (
    MockCallManager,
    MockEngine,
    MockRegistry,
    _spin_runner,
)


def _run(coro):
    return asyncio.run(coro)


def test_hangup_dispatches_to_call_manager() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hangup-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="reject-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hold-unhold-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="blind-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="wait-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_target_later() -> None:
            await asyncio.sleep(0.1)
            bus.emit(Event(type="user.my_signal"))

        scenario = Scenario(
            name="wait-until-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="emit-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hangup-all-smoke",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="hangup-all-global",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="dtmf-out-emission",
            phones=["a"],
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
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="meta-smoke",
            phones=["a"],
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


def test_wait_until_timeout_aborts_chain_with_action_failed() -> None:
    """wait_until with a timeout that never fires: chain must abort, the
    next action MUST NOT run, timeline MUST contain action.failed for
    wait_until."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="wait-until-timeout-smoke",
            phones=["a"],
            initial_actions=[
                {"action": "wait_until", "event": "user.never_fires", "timeout_ms": 100},
                {"action": "hangup", "phone_id": "a", "call_id": 9},
            ],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=2000,
        )
        result = await runner.run(scenario)
        # The hangup must NOT run — wait_until aborted the chain
        hangups = [c for c in cm.calls if c[0] == "hangup"]
        assert not hangups, f"hangup ran despite wait_until timeout: {cm.calls}"
        # Timeline records action.failed for wait_until
        failed = [
            e for e in result.timeline
            if e["kind"] == "meta" and e["type"] == "action.failed"
            and e.get("data", {}).get("name") == "wait_until"
        ]
        assert failed, (
            "expected action.failed meta entry for wait_until; "
            f"timeline meta: "
            f"{[e for e in result.timeline if e['kind'] == 'meta']}"
        )
        # Scenario hits its outer timeout — no stop_on match, no error status
        assert result.status == "timeout"

    _run(inner())


def test_set_codecs_in_hook_triggers_reinvite_on_inherited_call() -> None:
    """Hook on call.state.confirmed dispatching set_codecs without explicit
    phone_id/call_id null-suppression: must call engine.set_codecs AND
    cm.unhold (re-INVITE) on the inherited call."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        eng = MockEngine()
        runner = _spin_runner(cm, MockRegistry(), eng, bus, loop)

        async def emit_confirmed_soon() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.confirmed", phone_id="a", call_id=42))

        scenario = Scenario(
            name="set-codecs-reinvite-smoke",
            phones=["a"],
            hooks=[{
                "when": "call.state.confirmed",
                "on_phone": "a",
                "once": True,
                "then": [{"action": "set_codecs", "codecs": ["G722", "PCMA"]}],
                # Note: no explicit phone_id/call_id; they should be inherited
                # from hook.on_phone="a" and event.call_id=42 → re-INVITE path.
            }],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=1500,
        )
        task = asyncio.create_task(emit_confirmed_soon())
        await runner.run(scenario)
        await task
        # 1) Endpoint-wide codec change happened
        sc = [c for c in eng.calls if c[0] == "set_codecs"]
        assert sc and sc[0][1]["codecs"] == ["G722", "PCMA"], (
            f"set_codecs not called or wrong codecs: {eng.calls}"
        )
        # 2) re-INVITE happened — CallManager.unhold dispatched with
        # the inherited call_id and phone_id
        unholds = [c for c in cm.calls if c[0] == "unhold"]
        assert unholds, f"re-INVITE (cm.unhold) was not called: {cm.calls}"
        assert unholds[0][1]["call_id"] == 42
        assert unholds[0][1]["phone_id"] == "a"

    _run(inner())


def test_set_codecs_with_null_phone_and_call_skips_reinvite() -> None:
    """Hook on call.state.confirmed dispatching set_codecs with explicit
    phone_id: None, call_id: None: must call engine.set_codecs but
    NOT cm.unhold (re-INVITE suppressed). Canonical idiom for endpoint-wide
    codec change inside a hook (see SKILL.md / reference.md)."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        eng = MockEngine()
        runner = _spin_runner(cm, MockRegistry(), eng, bus, loop)

        async def emit_confirmed_soon() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.confirmed", phone_id="a", call_id=42))

        scenario = Scenario(
            name="set-codecs-null-suppress-smoke",
            phones=["a"],
            hooks=[{
                "when": "call.state.confirmed",
                "on_phone": "a",
                "once": True,
                "then": [{
                    "action": "set_codecs",
                    "codecs": ["PCMU"],
                    "phone_id": None,
                    "call_id": None,
                }],
            }],
            stop_on=[{"phone_id": "a", "event": "user.done"}],
            timeout_ms=1500,
        )
        task = asyncio.create_task(emit_confirmed_soon())
        await runner.run(scenario)
        await task
        # 1) Endpoint-wide codec change happened
        sc = [c for c in eng.calls if c[0] == "set_codecs"]
        assert sc and sc[0][1]["codecs"] == ["PCMU"]
        # 2) NO re-INVITE — cm.unhold must NOT have been called
        unholds = [c for c in cm.calls if c[0] == "unhold"]
        assert not unholds, (
            f"re-INVITE happened despite explicit nulls — suppression broken: "
            f"{cm.calls}"
        )

    _run(inner())


# ============================================================================
# Block 4 — Group B (event/data semantics), Group C (action argument coverage),
# Group D (defaults inheritance corner cases).
# ============================================================================


def test_emit_data_payload_filters_downstream_hooks() -> None:
    """Producer emit({name, data: {origin: 1}}) — consumer matches on data;
    only the matching consumer fires."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="emit-data-flow",
            phones=["a"],
            initial_actions=[
                {"action": "emit", "name": "stage", "data": {"origin": 1}},
                {"action": "emit", "name": "stage", "data": {"origin": 2}},
            ],
            hooks=[{
                "when": "user.stage",
                "match": {"origin": 2},
                "once": True,
                "then": [{"action": "emit", "name": "matched_two"}],
            }],
            stop_on=[{"event": "user.matched_two"}],
            timeout_ms=500,
        )
        result = await runner.run(scenario)
        assert result.status == "ok", (
            f"emit→match→emit chain failed: {result.reason}"
        )

    _run(inner())


def test_attended_transfer_auto_pick_passes_none_call_ids() -> None:
    """attended_transfer with no call_id/dest_call_id args dispatches with
    both as None — letting CallManager auto-pick the active legs."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="attended-auto",
            phones=["b"],
            initial_actions=[
                {"action": "attended_transfer", "phone_id": "b"},
            ],
            stop_on=[{"event": "user.done"}],
            timeout_ms=200,
        )
        await runner.run(scenario)
        xfer = [c for c in cm.calls if c[0] == "attended_transfer"]
        assert xfer, "attended_transfer not dispatched"
        assert xfer[0][1]["call_id"] is None
        assert xfer[0][1]["dest_call_id"] is None

    _run(inner())


def test_make_call_headers_propagate() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="headers-smoke",
            phones=["a"],
            initial_actions=[{
                "action": "make_call",
                "phone_id": "a",
                "to": "sip:x@asterisk",
                "headers": {"X-Reason": "consult"},
            }],
            stop_on=[{"event": "user.done"}],
            timeout_ms=200,
        )
        await runner.run(scenario)
        mk = [c for c in cm.calls if c[0] == "make_call"]
        assert mk and mk[0][1]["dest_uri"] == "sip:x@asterisk"
        # MockCallManager.make_call doesn't currently track headers — extend
        # MockCallManager.make_call to record headers too if needed.

    _run(inner())


def test_make_call_from_phone_alias() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="from-phone-alias",
            phones=["a"],
            initial_actions=[{
                "action": "make_call",
                "from_phone": "a",   # alias for phone_id
                "to": "sip:y@asterisk",
            }],
            stop_on=[{"event": "user.done"}],
            timeout_ms=200,
        )
        await runner.run(scenario)
        mk = [c for c in cm.calls if c[0] == "make_call"]
        assert mk and mk[0][1]["phone_id"] == "a"

    _run(inner())


def test_play_audio_loop_true_propagates() -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="play-loop",
            phones=["a"],
            initial_actions=[{
                "action": "play_audio",
                "phone_id": "a",
                "call_id": 1,
                "file": "/audio/moh.wav",
                "loop": True,
            }],
            stop_on=[{"event": "user.done"}],
            timeout_ms=100,
        )
        await runner.run(scenario)
        play = [c for c in cm.calls if c[0] == "play_audio"]
        assert play and play[0][1]["loop"] is True

    _run(inner())


@pytest.mark.parametrize("bad_action", [
    {"action": "send_dtmf", "phone_id": "a", "call_id": 1},      # missing digits
    {"action": "blind_transfer", "phone_id": "a", "call_id": 1}, # missing to
    {"action": "make_call", "to": "sip:x"},                      # missing phone_id
    {"action": "make_call", "phone_id": "a"},                    # missing to
    {"action": "play_audio", "phone_id": "a", "call_id": 1},     # missing file
    {"action": "send_message", "phone_id": "a"},                 # missing to+body
])
def test_missing_required_arg_records_action_failed(bad_action) -> None:
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        scenario = Scenario(
            name="missing-arg-smoke",
            phones=["a"],
            initial_actions=[bad_action],
            stop_on=[{"event": "user.done"}],
            timeout_ms=200,
        )
        # skip_validation=True so the action reaches the executor
        result = await runner.run(scenario, skip_validation=True)
        failed = [
            e for e in result.timeline
            if e["kind"] == "meta" and e["type"] == "action.failed"
        ]
        assert failed, f"expected action.failed in timeline; got {result.timeline}"

    _run(inner())


def test_phone_id_falls_through_from_event_when_hook_has_no_on_phone() -> None:
    """Hook without on_phone — action's phone_id is inherited from
    event.phone_id."""
    async def inner() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        cm = MockCallManager(bus)
        runner = _spin_runner(cm, MockRegistry(), MockEngine(), bus, loop)

        async def emit_confirmed_on_b() -> None:
            await asyncio.sleep(0.05)
            bus.emit(Event(type="call.state.confirmed", phone_id="b", call_id=7))

        scenario = Scenario(
            name="event-phone-fallthrough",
            phones=["b"],
            hooks=[{
                "when": "call.state.confirmed",
                "once": True,
                "then": ["hangup"],
            }],
            stop_on=[{"event": "user.done"}],
            timeout_ms=1500,
        )
        task = asyncio.create_task(emit_confirmed_on_b())
        await runner.run(scenario)
        await task
        hu = [c for c in cm.calls if c[0] == "hangup"]
        assert hu and hu[0][1]["phone_id"] == "b" and hu[0][1]["call_id"] == 7

    _run(inner())
