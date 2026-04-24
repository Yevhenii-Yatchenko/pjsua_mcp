"""Unit tests for validate_scenario — static checks without pjsua/MCP."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.scenario_engine.pattern_loader import PatternRegistry
from src.scenario_engine.validator import (
    KNOWN_ACTIONS,
    _is_known_event,
    validate_scenario,
)

PATTERNS_DIR = Path(__file__).resolve().parents[2] / "scenarios" / "patterns"


@pytest.fixture(scope="module")
def registry() -> PatternRegistry:
    r = PatternRegistry(PATTERNS_DIR)
    r.scan()
    return r


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

    # Instantiate a throwaway executor with stubs just to read dispatch keys.
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


def test_valid_scenario_passes(registry: PatternRegistry) -> None:
    scenario = {
        "name": "ok-scenario",
        "phones": ["a", "b"],
        "patterns": [
            {"use": "auto-answer", "phone_id": "b"},
            {"use": "make-call-and-wait-confirmed", "phone_id": "a",
             "dest_uri": "sip:6002@asterisk"},
        ],
        "hooks": [
            {"when": "call.state.confirmed", "on_phone": "a",
             "then": [{"wait": "1s"}, "hangup"]},
        ],
        "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "ok", f"expected ok, got {report}"
    assert report["issues"] == []
    assert "auto-answer" in report["patterns_used"]


def test_unknown_pattern_detected(registry: PatternRegistry) -> None:
    scenario = {
        "name": "bad-pattern",
        "patterns": [{"use": "does-not-exist", "phone_id": "a"}],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "error"
    issues = report["issues"]
    assert any(i["kind"] == "pattern_ref" and "does-not-exist" in i["msg"]
               for i in issues)


def test_pattern_param_validation_fails(registry: PatternRegistry) -> None:
    scenario = {
        "name": "bad-params",
        # phone_id must match ^[a-z0-9_]{1,32}$
        "patterns": [{"use": "auto-answer", "phone_id": "INVALID-CAPS"}],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "error"


def test_unknown_action_in_hook_detected(registry: PatternRegistry) -> None:
    scenario = {
        "name": "bad-action",
        "hooks": [
            {"when": "call.state.confirmed",
             "then": [{"frobnicate_extra": "xyz"}]},
        ],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "error"
    issues = report["issues"]
    assert any(i["kind"] == "action" and "frobnicate_extra" in i["msg"]
               for i in issues)


def test_unknown_event_in_when_detected(registry: PatternRegistry) -> None:
    scenario = {
        "hooks": [
            {"when": "nonexistent.event", "then": ["hangup"]},
        ],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "error"
    assert any(i["kind"] == "hook" and "nonexistent.event" in i["msg"]
               for i in report["issues"])


def test_unknown_event_in_stop_on_detected(registry: PatternRegistry) -> None:
    scenario = {
        "stop_on": [{"event": "totally.fake"}],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "error"
    assert any(i["kind"] == "stop_on" and "totally.fake" in i["msg"]
               for i in report["issues"])


def test_unknown_action_in_initial_actions_detected(registry: PatternRegistry) -> None:
    scenario = {
        "initial_actions": [{"nonexistent_action": 42}],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "error"
    assert any(i["kind"] == "initial_action" and "nonexistent_action" in i["msg"]
               for i in report["issues"])


def test_hook_missing_when_detected(registry: PatternRegistry) -> None:
    scenario = {
        "hooks": [{"then": ["hangup"]}],
    }
    report = validate_scenario(scenario, registry)
    assert report["status"] == "error"
    assert any(i["kind"] == "hook" and "missing `when:`" in i["msg"]
               for i in report["issues"])


def test_all_example_scenarios_validate(registry: PatternRegistry) -> None:
    """Every YAML in scenarios/examples/ must pass static validation."""
    from src.scenario_engine.orchestrator import Scenario

    examples = (PATTERNS_DIR.parent / "examples").glob("*.yaml")
    failures = []
    for p in examples:
        try:
            scn = Scenario.from_yaml_file(p)
        except Exception as e:
            failures.append((p.name, f"parse: {e}"))
            continue
        report = validate_scenario(scn, registry)
        if report["status"] != "ok":
            failures.append((p.name, report["issues"]))
    assert not failures, f"example scenarios failed validation: {failures}"
