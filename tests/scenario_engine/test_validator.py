"""Unit tests for validate_scenario — static checks without pjsua/MCP."""

from __future__ import annotations

from src.scenario_engine.validator import (
    KNOWN_ACTIONS,
    _is_known_event,
    validate_scenario,
)


def test_is_known_event_recognises_known_prefixes() -> None:
    assert _is_known_event("reg.success")
    assert _is_known_event("call.state.confirmed")
    assert _is_known_event("dtmf.in")
    assert _is_known_event("user.anything_goes_here")
    assert _is_known_event("scenario.stopped")


def test_is_known_event_rejects_junk() -> None:
    assert not _is_known_event("callstate.confirmed")    # no dot
    assert not _is_known_event("bogus.thing")
    assert not _is_known_event("FOO")


def test_is_known_event_accepts_wildcards() -> None:
    assert _is_known_event("*")
    assert _is_known_event("call.state.*")


def test_known_actions_matches_executor_dispatch() -> None:
    """If ActionExecutor adds a new action, KNOWN_ACTIONS must keep up."""
    from src.scenario_engine.action_executor import ActionExecutor

    class _Stub:
        pass
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        exe = ActionExecutor(_Stub(), _Stub(), _Stub(), _Stub(), loop, None)  # type: ignore[arg-type]
        dispatch_keys = set(exe._dispatch.keys())
    finally:
        loop.close()
    assert dispatch_keys == set(KNOWN_ACTIONS), (
        f"KNOWN_ACTIONS drift: "
        f"dispatch has {dispatch_keys - KNOWN_ACTIONS}, "
        f"validator has {KNOWN_ACTIONS - dispatch_keys}"
    )


def test_valid_scenario_passes() -> None:
    scenario = {
        "name": "ok-scenario",
        "phones": ["a", "b"],
        "hooks": [
            {"when": "call.state.incoming", "on_phone": "b",
             "then": [{"wait": "200ms"}, "answer"]},
            {"when": "call.state.confirmed", "on_phone": "a",
             "then": [{"wait": "1s"}, "hangup"]},
        ],
        "initial_actions": [
            {"action": "make_call", "phone_id": "a", "dest_uri": "sip:6002@asterisk"},
        ],
        "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    }
    report = validate_scenario(scenario)
    assert report["status"] == "ok", f"expected ok, got {report}"
    assert report["issues"] == []


def test_unknown_action_in_hook_detected() -> None:
    scenario = {
        "name": "bad-action",
        "hooks": [
            {"when": "call.state.confirmed",
             "then": [{"frobnicate_extra": "xyz"}]},
        ],
    }
    report = validate_scenario(scenario)
    assert report["status"] == "error"
    issues = report["issues"]
    assert any(i["kind"] == "action" and "frobnicate_extra" in i["msg"]
               for i in issues)


def test_unknown_event_in_when_detected() -> None:
    scenario = {
        "hooks": [
            {"when": "nonexistent.event", "then": ["hangup"]},
        ],
    }
    report = validate_scenario(scenario)
    assert report["status"] == "error"
    assert any(i["kind"] == "hook" and "nonexistent.event" in i["msg"]
               for i in report["issues"])


def test_unknown_event_in_stop_on_detected() -> None:
    scenario = {
        "stop_on": [{"event": "totally.fake"}],
    }
    report = validate_scenario(scenario)
    assert report["status"] == "error"
    assert any(i["kind"] == "stop_on" and "totally.fake" in i["msg"]
               for i in report["issues"])


def test_unknown_action_in_initial_actions_detected() -> None:
    scenario = {
        "initial_actions": [{"nonexistent_action": 42}],
    }
    report = validate_scenario(scenario)
    assert report["status"] == "error"
    assert any(i["kind"] == "initial_action" and "nonexistent_action" in i["msg"]
               for i in report["issues"])


def test_hook_missing_when_detected() -> None:
    scenario = {
        "hooks": [{"then": ["hangup"]}],
    }
    report = validate_scenario(scenario)
    assert report["status"] == "error"
    assert any(i["kind"] == "hook" and "missing `when:`" in i["msg"]
               for i in report["issues"])
