"""Scenario orchestrator — arms hooks from patterns, runs initial actions, awaits terminal.

run_scenario() is the top-level entry point: given a scenario dict (plus managers),
it instantiates patterns, arms hooks, executes initial actions, and awaits either
a stop_on match or the timeout.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from src.scenario_engine.action_executor import ActionExecutor
from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.hook_runtime import Hook, HookRuntime, _event_matches_predicates
from src.scenario_engine.pattern_loader import Pattern, PatternError, PatternRegistry
from src.scenario_engine.timeline import Timeline, TimelineRecorder

if TYPE_CHECKING:
    from src.account_manager import PhoneRegistry
    from src.call_manager import CallManager
    from src.sip_engine import SipEngine


class ScenarioError(Exception):
    pass


@dataclass
class Scenario:
    name: str = "<anonymous>"
    description: str = ""
    phones: list[str] = field(default_factory=list)
    patterns: list[dict[str, Any]] = field(default_factory=list)
    hooks: list[dict[str, Any]] = field(default_factory=list)
    initial_actions: list[Any] = field(default_factory=list)
    stop_on: list[dict[str, Any]] = field(default_factory=list)
    timeout_ms: int = 60000

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Scenario":
        return cls(
            name=str(d.get("name", "<anonymous>")),
            description=str(d.get("description", "")),
            phones=list(d.get("phones", []) or []),
            patterns=[dict(p) for p in (d.get("patterns") or [])],
            hooks=[dict(h) for h in (d.get("hooks") or [])],
            initial_actions=list(d.get("initial_actions") or []),
            stop_on=[dict(s) for s in (d.get("stop_on") or [])],
            timeout_ms=int(d.get("timeout_ms", 60000)),
        )

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> "Scenario":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data)


@dataclass
class ScenarioResult:
    status: str  # "ok" | "timeout" | "error"
    reason: str
    elapsed_ms: float
    timeline: list[dict[str, Any]]
    errors: list[dict[str, Any]] = field(default_factory=list)
    patterns_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "timeline": list(self.timeline),
            "errors": list(self.errors),
            "patterns_used": list(self.patterns_used),
        }


def _strip_version_suffix(name: str) -> str:
    """'auto-answer@^1.0.0' -> 'auto-answer'."""
    return name.split("@", 1)[0]


class ScenarioRunner:
    """Orchestrates a single scenario run against pjsua managers."""

    def __init__(
        self,
        bus: EventBus,
        pattern_registry: PatternRegistry,
        call_manager: "CallManager",
        registry: "PhoneRegistry",
        loop: asyncio.AbstractEventLoop,
        engine: "SipEngine | None" = None,
    ) -> None:
        self._bus = bus
        self._patterns = pattern_registry
        self._cm = call_manager
        self._registry = registry
        self._loop = loop
        self._engine = engine

    async def run(
        self,
        scenario: Scenario,
        skip_validation: bool = False,
    ) -> ScenarioResult:
        t_start = time.monotonic()
        errors: list[dict[str, Any]] = []
        patterns_used: list[str] = []

        # ---- Pre-flight static validation ----
        # Catches typos, unknown patterns, unknown actions, malformed hooks
        # BEFORE we waste a timeout on silent failures. Bypassable via
        # `skip_validation=True` for engine-level tests that inject invalid
        # specs on purpose. Lazy import to dodge a circular dependency
        # (validator → orchestrator.Scenario; orchestrator → validator).
        if not skip_validation:
            from src.scenario_engine.validator import (
                validate_scenario as _static_validate,
            )
            report = _static_validate(scenario, self._patterns)
            if report["status"] == "error":
                timeline = Timeline(t0=t_start)
                recorder = TimelineRecorder(self._bus, timeline)
                recorder.start()
                recorder.record_meta("scenario.validation_failed", {
                    "issues": report["issues"],
                    "name": scenario.name,
                })
                recorder.stop()
                return ScenarioResult(
                    status="error",
                    reason="pre-flight validation failed",
                    elapsed_ms=(time.monotonic() - t_start) * 1000,
                    timeline=timeline.to_list(),
                    errors=[{"stage": "validation", **i} for i in report["issues"]],
                    patterns_used=list(report.get("patterns_used") or []),
                )

        timeline = Timeline(t0=t_start)
        recorder = TimelineRecorder(self._bus, timeline)
        recorder.start()
        recorder.record_meta("scenario.started", {"name": scenario.name, "phones": scenario.phones})
        self._bus.emit(Event(type="scenario.started", data={"name": scenario.name}))

        executor = ActionExecutor(
            call_manager=self._cm,
            registry=self._registry,
            bus=self._bus,
            recorder=recorder,
            loop=self._loop,
            engine=self._engine,
        )
        runtime = HookRuntime(self._bus, executor.execute, self._loop)

        # ---- Load + arm patterns ----
        instantiated: list[Pattern] = []
        for pat_ref in scenario.patterns:
            ref = dict(pat_ref)
            use = ref.pop("use", None)
            if not use:
                errors.append({"pattern_ref": pat_ref, "error": "missing `use:` key"})
                continue
            bare = _strip_version_suffix(str(use))
            try:
                pat = self._patterns.instantiate(bare, ref)
            except PatternError as e:
                errors.append({"pattern_ref": pat_ref, "error": str(e)})
                recorder.record_meta("pattern.error", {"pattern": use, "error": str(e)})
                continue
            instantiated.append(pat)
            patterns_used.append(f"{pat.name}@{pat.version}")
            for hspec in pat.hooks:
                try:
                    runtime.arm(hspec, pattern_name=pat.name)
                except Exception as exc:  # noqa: BLE001
                    errors.append({
                        "pattern": pat.name,
                        "hook_spec": hspec,
                        "error": repr(exc),
                    })

        # ---- Arm scenario-level inline hooks ----
        # These live in the scenario YAML directly, without needing a
        # pattern wrapper — useful for one-off flow logic that isn't worth
        # abstracting into a library pattern.
        for hspec in scenario.hooks:
            try:
                runtime.arm(hspec, pattern_name="<scenario>")
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "stage": "scenario_hooks",
                    "hook_spec": hspec,
                    "error": repr(exc),
                })

        if errors and not instantiated and not scenario.hooks:
            recorder.record_meta("scenario.aborted", {"reason": "all patterns failed to load"})
            recorder.stop()
            return ScenarioResult(
                status="error",
                reason="all patterns failed to load",
                elapsed_ms=(time.monotonic() - t_start) * 1000,
                timeline=timeline.to_list(),
                errors=errors,
                patterns_used=patterns_used,
            )

        # ---- Stop condition setup (BEFORE running initial actions) ----
        stop_future: asyncio.Future[Event] = self._loop.create_future()
        stop_sub_ids: list[int] = []

        def make_stop_handler(spec: dict[str, Any]) -> Any:
            expected_phone = spec.get("phone_id")
            expected_call_id = spec.get("call_id")
            match_preds = dict(spec.get("match") or {})

            def handler(ev: Event) -> None:
                if expected_phone is not None and ev.phone_id != expected_phone:
                    return
                if expected_call_id is not None and ev.call_id != expected_call_id:
                    return
                if match_preds and not _event_matches_predicates(ev, match_preds):
                    return
                if stop_future.done():
                    return
                self._loop.call_soon_threadsafe(stop_future.set_result, ev)

            return handler

        for stop_spec in scenario.stop_on:
            ev_type = stop_spec.get("event")
            if not ev_type:
                continue
            sub_id = self._bus.subscribe(ev_type, make_stop_handler(stop_spec))
            stop_sub_ids.append(sub_id)

        # ---- Initial actions ----
        init_actions: list[Any] = list(scenario.initial_actions)
        for p in instantiated:
            init_actions.extend(p.initial_actions)

        if init_actions:
            fake_hook = Hook(
                hook_id="<init>",
                when=[],
                on_phone=None,
                match={},
                then=init_actions,
                once=True,
                pattern_name="<scenario>",
            )
            fake_event = Event(type="scenario.started")
            try:
                await executor.execute(init_actions, fake_hook, fake_event)
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "initial_actions", "error": repr(exc)})
                recorder.record_meta("initial.error", {"error": repr(exc)})

        # ---- Await stop condition or timeout ----
        status = "ok"
        reason = "stop_on matched"
        if not scenario.stop_on:
            # No explicit stop → just wait full timeout
            try:
                await asyncio.sleep(scenario.timeout_ms / 1000.0)
            except asyncio.CancelledError:
                pass
            status = "ok"
            reason = "full duration (no stop_on defined)"
        else:
            try:
                await asyncio.wait_for(stop_future, timeout=scenario.timeout_ms / 1000.0)
            except asyncio.TimeoutError:
                status = "timeout"
                reason = f"timeout after {scenario.timeout_ms}ms"

        # ---- Cleanup ----
        for sid in stop_sub_ids:
            self._bus.unsubscribe(sid)
        runtime.remove_all()
        self._bus.emit(Event(type="scenario.stopped", data={"status": status, "reason": reason}))
        recorder.record_meta("scenario.stopped", {"status": status, "reason": reason})
        recorder.stop()

        elapsed_ms = (time.monotonic() - t_start) * 1000
        return ScenarioResult(
            status=status,
            reason=reason,
            elapsed_ms=elapsed_ms,
            timeline=timeline.to_list(),
            errors=errors,
            patterns_used=patterns_used,
        )


async def run_scenario(
    scenario: dict[str, Any] | str | Path | Scenario,
    *,
    bus: EventBus,
    pattern_registry: PatternRegistry,
    call_manager: "CallManager",
    registry: "PhoneRegistry",
    loop: asyncio.AbstractEventLoop | None = None,
    engine: "SipEngine | None" = None,
    skip_validation: bool = False,
) -> ScenarioResult:
    """Top-level helper: accepts a dict / YAML path / Scenario and runs it.

    By default the scenario is statically validated first; on any validation
    error, the run returns `status="error"` immediately without touching
    pjsua. Pass `skip_validation=True` to bypass (used by engine tests only).
    """
    if loop is None:
        loop = asyncio.get_running_loop()
    if isinstance(scenario, Scenario):
        scn = scenario
    elif isinstance(scenario, dict):
        scn = Scenario.from_dict(scenario)
    else:
        scn = Scenario.from_yaml_file(scenario)
    runner = ScenarioRunner(
        bus=bus,
        pattern_registry=pattern_registry,
        call_manager=call_manager,
        registry=registry,
        loop=loop,
        engine=engine,
    )
    return await runner.run(scn, skip_validation=skip_validation)
