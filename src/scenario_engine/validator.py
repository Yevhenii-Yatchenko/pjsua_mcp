"""Static validation for scenarios — catch typos before `run_scenario`.

Pure functions, no asyncio, no pjsua — safe to call from unit tests or
from the `validate_scenario` MCP tool without side effects.
"""

from __future__ import annotations

from typing import Any

from src.scenario_engine.action_executor import normalize_action
from src.scenario_engine.orchestrator import Scenario
from src.scenario_engine.pattern_loader import PatternError, PatternRegistry

KNOWN_EVENT_PREFIXES: tuple[str, ...] = (
    "reg.", "call.state.", "dtmf.", "im.", "scenario.", "user.", "timer.",
    "sip.request.", "sip.response.", "media.",
)

# Actions implemented in ActionExecutor — MUST stay in sync with its dispatch.
KNOWN_ACTIONS: frozenset[str] = frozenset({
    # Call control
    "answer", "hangup", "hangup_all", "reject", "hold", "unhold",
    "send_dtmf", "blind_transfer", "attended_transfer", "conference", "make_call",
    # Media
    "play_audio", "stop_audio",
    # Messaging
    "send_message",
    # Codec
    "set_codecs",
    # Flow control
    "wait", "wait_until", "emit", "checkpoint", "log",
})


def _is_known_event(ev: str) -> bool:
    if ev == "*":
        return True
    if ev.endswith(".*"):
        base = ev[:-2]
        return any(base.startswith(p.rstrip(".")) or base + "." == p for p in KNOWN_EVENT_PREFIXES) or base == "scenario"
    return any(ev.startswith(p) for p in KNOWN_EVENT_PREFIXES)


def _check_action_spec(spec: Any) -> tuple[str, str | None]:
    """Returns (action_name, error_message_or_None)."""
    try:
        name, _ = normalize_action(spec)
    except Exception as e:  # noqa: BLE001
        return ("", f"bad action spec: {e}")
    if name not in KNOWN_ACTIONS:
        return (name, f"unknown action: {name!r}")
    return (name, None)


def validate_scenario(
    scenario: dict[str, Any] | Scenario,
    pattern_registry: PatternRegistry,
) -> dict[str, Any]:
    """Static check of a scenario. Returns a report:

        {
          status: "ok" | "error",
          issues: [{kind, ..., msg}, ...],
          patterns_used: [name, ...],
          scenario_name: str,
        }
    """
    if isinstance(scenario, Scenario):
        scn = scenario
    else:
        try:
            scn = Scenario.from_dict(scenario)
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "issues": [{"kind": "parse", "msg": str(e)}],
                "patterns_used": [],
                "scenario_name": "<parse-failed>",
            }

    issues: list[dict[str, Any]] = []
    patterns_used: list[str] = []

    # 1. Pattern refs
    for i, pref in enumerate(scn.patterns):
        use = pref.get("use")
        if not use:
            issues.append({"kind": "pattern_ref", "index": i, "msg": "missing `use:` key"})
            continue
        bare = str(use).split("@", 1)[0]
        args = {k: v for k, v in pref.items() if k != "use"}
        try:
            pattern_registry.instantiate(bare, args)
            patterns_used.append(bare)
        except PatternError as e:
            issues.append({"kind": "pattern_ref", "index": i, "use": use, "msg": str(e)})

    # 2. Inline hooks (when + then)
    def _check_hook(h: dict[str, Any], source: str, idx: int) -> None:
        when = h.get("when")
        if not when:
            issues.append({"kind": "hook", "source": source, "index": idx, "msg": "missing `when:`"})
            return
        whens = [when] if isinstance(when, str) else list(when)
        for w in whens:
            if not _is_known_event(str(w)):
                issues.append({"kind": "hook", "source": source, "index": idx,
                               "msg": f"unknown event type: {w!r}"})
        for j, spec in enumerate(h.get("then", []) or []):
            _, err = _check_action_spec(spec)
            if err:
                issues.append({"kind": "action", "source": source, "hook": idx,
                               "step": j, "msg": err})

    for i, h in enumerate(scn.hooks):
        _check_hook(h, "scenario.hooks", i)

    # 3. stop_on
    for i, s in enumerate(scn.stop_on):
        ev = s.get("event")
        if ev is not None and not _is_known_event(str(ev)):
            issues.append({"kind": "stop_on", "index": i,
                           "msg": f"unknown event type: {ev!r}"})

    # 4. initial_actions
    for i, spec in enumerate(scn.initial_actions):
        _, err = _check_action_spec(spec)
        if err:
            issues.append({"kind": "initial_action", "index": i, "msg": err})

    return {
        "status": "ok" if not issues else "error",
        "issues": issues,
        "patterns_used": patterns_used,
        "scenario_name": scn.name,
    }
