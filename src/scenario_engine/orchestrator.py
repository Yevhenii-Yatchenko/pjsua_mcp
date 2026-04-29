"""Scenario orchestrator — arms inline hooks, runs initial actions, awaits terminal.

run_scenario() is the top-level entry point: given a scenario dict (plus managers),
it arms hooks, executes initial actions, and awaits either a stop_on match or the
timeout.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.scenario_engine.action_executor import ActionExecutor
from src.scenario_engine.artifacts import collect_artifacts
from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.hook_runtime import Hook, HookRuntime, _event_matches_predicates
from src.scenario_engine.timeline import Timeline, TimelineRecorder

if TYPE_CHECKING:
    from pathlib import Path

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
            hooks=[dict(h) for h in (d.get("hooks") or [])],
            initial_actions=list(d.get("initial_actions") or []),
            stop_on=[dict(s) for s in (d.get("stop_on") or [])],
            timeout_ms=int(d.get("timeout_ms", 60000)),
        )


@dataclass
class ScenarioResult:
    status: str  # "ok" | "timeout" | "error"
    reason: str
    elapsed_ms: float
    timeline: list[dict[str, Any]]
    errors: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "timeline": list(self.timeline),
            "errors": list(self.errors),
            "artifacts": dict(self.artifacts),
        }


class ScenarioRunner:
    """Orchestrates a single scenario run against pjsua managers."""

    def __init__(
        self,
        bus: EventBus,
        call_manager: "CallManager",
        registry: "PhoneRegistry",
        loop: asyncio.AbstractEventLoop,
        engine: "SipEngine | None" = None,
        recordings_root: "Path | None" = None,
        captures_root: "Path | None" = None,
        host_recordings_root: str | None = None,
        host_captures_root: str | None = None,
    ) -> None:
        self._bus = bus
        self._cm = call_manager
        self._registry = registry
        self._loop = loop
        self._engine = engine
        self._recordings_root = recordings_root
        self._captures_root = captures_root
        self._host_recordings_root = host_recordings_root
        self._host_captures_root = host_captures_root

    async def run(
        self,
        scenario: Scenario,
        skip_validation: bool = False,
    ) -> ScenarioResult:
        t_start = time.monotonic()
        started_at_wall = time.time()
        errors: list[dict[str, Any]] = []

        # ---- Pre-flight static validation ----
        # Catches typos, unknown actions, malformed hooks BEFORE we waste a
        # timeout on silent failures. Bypassable via `skip_validation=True`
        # for engine-level tests that inject invalid specs on purpose. Lazy
        # import to dodge a circular dependency (validator → orchestrator.Scenario;
        # orchestrator → validator).
        if not skip_validation:
            from src.scenario_engine.validator import (
                validate_scenario as _static_validate,
            )
            report = _static_validate(scenario)
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
                )

        timeline = Timeline(t0=t_start)
        recorder = TimelineRecorder(self._bus, timeline)
        recorder.start()
        recorder.record_meta("scenario.started", {"name": scenario.name, "phones": scenario.phones})
        # NB: the `scenario.started` bus event is emitted AFTER all hooks +
        # stop_on subscribers are armed (see below). Otherwise a coordinator
        # hook with `when: scenario.started` would miss its own trigger.

        executor = ActionExecutor(
            call_manager=self._cm,
            registry=self._registry,
            bus=self._bus,
            recorder=recorder,
            loop=self._loop,
            engine=self._engine,
        )
        runtime = HookRuntime(self._bus, executor.execute, self._loop)

        # ---- Arm scenario-level inline hooks ----
        for hspec in scenario.hooks:
            try:
                runtime.arm(hspec, pattern_name="<scenario>")
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "stage": "scenario_hooks",
                    "hook_spec": hspec,
                    "error": repr(exc),
                })

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

        # ---- Replay registration state for pre-registered phones ----
        # `reg.success` fires once at add_phone time. If phones are
        # provisioned BEFORE run_scenario starts (the common case in our
        # test stand), patterns hooking on reg.success would silently time
        # out waiting for an event that already passed. Emit a synthetic
        # reg.success now so those hooks (and the timeline) see it. The
        # `synthetic: true` marker keeps this distinguishable from a real
        # registration cycle in debugging.
        if self._registry is not None:
            for pid in scenario.phones:
                get_info = getattr(self._registry, "get_registration_info", None)
                if get_info is None:
                    continue
                try:
                    info = get_info(pid)
                except Exception:  # noqa: BLE001
                    continue
                if not info.get("is_registered"):
                    continue
                self._bus.emit(Event(
                    type="reg.success",
                    phone_id=pid,
                    data={
                        "status_code": info.get("status_code", 200),
                        "reason": info.get("reason", ""),
                        "expires": info.get("expires", 0),
                        "synthetic": True,
                    },
                ))

        # ---- Now everything is wired: emit the scenario.started bus event ----
        # Hooks that listen for it (notably "coordinator" hooks chaining
        # wait_until to drive multi-stage flows) only see it if they were
        # armed first — see comment near recorder.record_meta above.
        self._bus.emit(Event(type="scenario.started", data={"name": scenario.name}))

        # ---- Initial actions ----
        init_actions: list[Any] = list(scenario.initial_actions)

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

        artifacts: dict[str, Any] = {}
        if (
            self._recordings_root is not None
            and self._captures_root is not None
            and scenario.phones
        ):
            try:
                artifacts = collect_artifacts(
                    phones=list(scenario.phones),
                    started_at=started_at_wall,
                    recordings_root=self._recordings_root,
                    captures_root=self._captures_root,
                    host_recordings_root=self._host_recordings_root,
                    host_captures_root=self._host_captures_root,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({"stage": "artifacts", "error": repr(exc)})

        return ScenarioResult(
            status=status,
            reason=reason,
            elapsed_ms=elapsed_ms,
            timeline=timeline.to_list(),
            errors=errors,
            artifacts=artifacts,
        )


async def run_scenario(
    scenario: dict[str, Any] | Scenario,
    *,
    bus: EventBus,
    call_manager: "CallManager",
    registry: "PhoneRegistry",
    loop: asyncio.AbstractEventLoop | None = None,
    engine: "SipEngine | None" = None,
    skip_validation: bool = False,
    recordings_root: "Path | None" = None,
    captures_root: "Path | None" = None,
    host_recordings_root: str | None = None,
    host_captures_root: str | None = None,
) -> ScenarioResult:
    """Top-level helper: accepts a dict or Scenario and runs it.

    By default the scenario is statically validated first; on any validation
    error, the run returns `status="error"` immediately without touching
    pjsua. Pass `skip_validation=True` to bypass (used by engine tests only).

    `recordings_root` / `captures_root` enable the post-run artifact sweep
    (sets `result.artifacts`). Optional `host_*_root` re-anchor the path
    strings that go into the response so out-of-container clients can
    Read them directly. When the roots are unset, `result.artifacts` is
    `{}` — no I/O is performed.
    """
    if loop is None:
        loop = asyncio.get_running_loop()
    if isinstance(scenario, Scenario):
        scn = scenario
    else:
        scn = Scenario.from_dict(scenario)
    runner = ScenarioRunner(
        bus=bus,
        call_manager=call_manager,
        registry=registry,
        loop=loop,
        engine=engine,
        recordings_root=recordings_root,
        captures_root=captures_root,
        host_recordings_root=host_recordings_root,
        host_captures_root=host_captures_root,
    )
    return await runner.run(scn, skip_validation=skip_validation)
