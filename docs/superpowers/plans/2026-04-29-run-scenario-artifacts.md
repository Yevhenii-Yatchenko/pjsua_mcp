# `run_scenario` Artifacts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Розширити `run_scenario` return shape новим полем `artifacts: {phone_id: {recording, recording_meta, pcap, host_recording, host_recording_meta, host_pcap}}` так, щоб клієнти могли отримати paths до WAV/pcap/meta.json одного scenario run без `list_recordings + ls -t` гімнастики.

**Architecture:** Pure-function `collect_artifacts()` у новому модулі `src/scenario_engine/artifacts.py` сканує `/recordings/<phone>/` та `/captures/<phone>/` за критерієм `mtime ≥ scenario.started_at`, повертає latest-by-mtime per phone. ScenarioRunner викликає її після `scenario.stopped` і прикладає до `ScenarioResult.artifacts`. Host-side path mapping вирішується env-vars `PJSUA_MCP_HOST_RECORDINGS_DIR` / `PJSUA_MCP_HOST_CAPTURES_DIR`, які docker-compose прокидає з `${CLAUDE_PLUGIN_DATA}/recordings` та `/captures` (або з host-side bind targets для bare-repo).

**Tech Stack:** Python 3.13, pjsua2, FastMCP. Без зовнішніх залежностей — лише `pathlib`, `os.stat`, `time`.

---

## Spec coverage map

Acceptance criterion → Task:
1. `artifacts.alice.recording` ≠ запис попереднього run → Task 1 (unit) + Task 5 (integration two-runs)
2. `host_pcap` — absolute host-side path → Task 1 (unit), Task 3 (server wiring)
3. Phones у `phones` без calls → `null` → Task 1, step 7
4. `recording_enabled: false` → `recording: null`, `pcap: set` (якщо capture_enabled) → Task 1, step 6
5. Зворотня сумісність API → Task 2, step 4 (existing fields cover, новий ключ default-empty)

---

## File structure

- **New:** `src/scenario_engine/artifacts.py` — pure-function `collect_artifacts()` + `ArtifactSet` dataclass.
- **New:** `tests/scenario_engine/test_artifacts.py` — unit tests (pure file-system fixtures).
- **Modify:** `src/scenario_engine/orchestrator.py` — `ScenarioRunner.__init__` accepts artifact roots, `run()` records `started_at_wall`, `ScenarioResult` gets `artifacts: dict[str, Any]` field, `to_dict()` includes it.
- **Modify:** `src/scenario_engine/__init__.py` — export `collect_artifacts`.
- **Modify:** `src/server.py` — read env vars, pass через `run_scenario_impl(...)` kwargs.
- **Modify:** `tests/scenario_engine/test_orchestrator.py` — додати `test_run_attaches_artifacts` з temp roots.
- **Modify:** `tests/test_integration.py` — новий `TestRunScenarioArtifacts` (live Asterisk).
- **Modify:** `docker-compose.yml` — environment блок з env vars + `.env.example`.
- **Modify:** `CHANGELOG.md` — Unreleased section.

---

## Task 1: Pure `collect_artifacts()` function

**Files:**
- Create: `src/scenario_engine/artifacts.py`
- Create: `tests/scenario_engine/test_artifacts.py`

- [ ] **Step 1: Write the failing test file**

Create `tests/scenario_engine/test_artifacts.py`:

```python
"""Unit tests for src.scenario_engine.artifacts.collect_artifacts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.scenario_engine.artifacts import collect_artifacts


def _touch(path: Path, mtime: float | None = None, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_returns_empty_dict_for_no_phones(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    rec.mkdir()
    cap.mkdir()
    out = collect_artifacts(
        phones=[],
        started_at=time.time(),
        recordings_root=rec,
        captures_root=cap,
    )
    assert out == {}


def test_phone_with_no_artifacts_is_null(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    rec.mkdir()
    cap.mkdir()
    started = time.time()
    out = collect_artifacts(
        phones=["alice", "bob"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out == {"alice": None, "bob": None}


def test_picks_recording_with_mtime_at_or_after_started_at(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    started = 1700000000.0
    # Old file — must be ignored
    _touch(rec / "alice" / "call_0_old.wav", mtime=started - 60)
    _touch(rec / "alice" / "call_0_old.meta.json", mtime=started - 60,
           content=b'{"call_id": 0}')
    # Fresh file — must be picked
    fresh = _touch(rec / "alice" / "call_1_new.wav", mtime=started + 5)
    fresh_meta = _touch(
        rec / "alice" / "call_1_new.meta.json",
        mtime=started + 5,
        content=b'{"call_id": 1}',
    )

    cap.mkdir(parents=True, exist_ok=True)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"]["recording"] == str(fresh)
    assert out["alice"]["recording_meta"] == str(fresh_meta)
    assert out["alice"]["pcap"] is None


def test_meta_null_when_sidecar_missing(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    started = 1700000000.0
    fresh = _touch(rec / "alice" / "call_1_new.wav", mtime=started + 5)
    cap.mkdir(parents=True, exist_ok=True)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"]["recording"] == str(fresh)
    assert out["alice"]["recording_meta"] is None


def test_picks_pcap_with_mtime_at_or_after_started_at(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    rec.mkdir()
    started = 1700000000.0
    _touch(cap / "alice" / "call_0_old.pcap", mtime=started - 60)
    fresh_pcap = _touch(cap / "alice" / "call_1_new.pcap", mtime=started + 5)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"]["pcap"] == str(fresh_pcap)
    assert out["alice"]["recording"] is None


def test_latest_wins_when_multiple_fresh(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    cap.mkdir()
    started = 1700000000.0
    _touch(rec / "alice" / "call_1.wav", mtime=started + 5)
    newest = _touch(rec / "alice" / "call_2.wav", mtime=started + 10)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"]["recording"] == str(newest)


def test_recording_disabled_pcap_only(tmp_path):
    """Phone with capture_enabled=true but recording_enabled=false →
    recording=None, pcap=set."""
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    rec.mkdir()
    started = 1700000000.0
    fresh_pcap = _touch(cap / "alice" / "call_0.pcap", mtime=started + 5)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"]["recording"] is None
    assert out["alice"]["recording_meta"] is None
    assert out["alice"]["pcap"] == str(fresh_pcap)


def test_phone_without_directory_is_null(tmp_path):
    """No `/recordings/alice/` and no `/captures/alice/` → null entry."""
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    rec.mkdir()
    cap.mkdir()
    started = 1700000000.0
    # Only bob has files; alice has no directory at all.
    _touch(rec / "bob" / "call_0.wav", mtime=started + 5)

    out = collect_artifacts(
        phones=["alice", "bob"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"] is None
    assert out["bob"] is not None


def test_host_paths_set_when_host_roots_provided(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    started = 1700000000.0
    fresh_wav = _touch(rec / "alice" / "call_0.wav", mtime=started + 5)
    fresh_meta = _touch(rec / "alice" / "call_0.meta.json", mtime=started + 5)
    fresh_pcap = _touch(cap / "alice" / "call_0.pcap", mtime=started + 5)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
        host_recordings_root="/host/data/recordings",
        host_captures_root="/host/data/captures",
    )
    a = out["alice"]
    assert a["host_recording"] == "/host/data/recordings/alice/call_0.wav"
    assert a["host_recording_meta"] == "/host/data/recordings/alice/call_0.meta.json"
    assert a["host_pcap"] == "/host/data/captures/alice/call_0.pcap"


def test_host_paths_null_when_host_roots_absent(tmp_path):
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    started = 1700000000.0
    _touch(rec / "alice" / "call_0.wav", mtime=started + 5)
    cap.mkdir(parents=True, exist_ok=True)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    a = out["alice"]
    assert a["recording"] is not None
    assert a["host_recording"] is None
    assert a["host_recording_meta"] is None
    assert a["host_pcap"] is None


def test_old_only_files_yield_null(tmp_path):
    """All artifacts pre-date started_at — phone gets null."""
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    started = 1700000000.0
    _touch(rec / "alice" / "old.wav", mtime=started - 60)
    _touch(cap / "alice" / "old.pcap", mtime=started - 60)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"] is None


def test_recording_pairs_with_correct_meta_sidecar(tmp_path):
    """Latest-mtime recording's `<stem>.meta.json` is preferred over a
    different recording's meta — pairing must follow stem, not mtime alone."""
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    cap.mkdir()
    started = 1700000000.0
    # Two recordings, both fresh; meta only for the latest one.
    _touch(rec / "alice" / "call_1.wav", mtime=started + 5)
    newest = _touch(rec / "alice" / "call_2.wav", mtime=started + 10)
    newest_meta = _touch(rec / "alice" / "call_2.meta.json", mtime=started + 10)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
    )
    assert out["alice"]["recording"] == str(newest)
    assert out["alice"]["recording_meta"] == str(newest_meta)
```

- [ ] **Step 2: Run test file to verify it fails (module not found)**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/scenario_engine/test_artifacts.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'src.scenario_engine.artifacts'`.

- [ ] **Step 3: Implement `collect_artifacts`**

Create `src/scenario_engine/artifacts.py`:

```python
"""Per-phone artifact collection for scenario runs.

After a scenario stops, sweep `<recordings_root>/<phone_id>/*.wav` and
`<captures_root>/<phone_id>/*.pcap` for files created during this run
(mtime ≥ scenario.started_at). Each phone reports the latest-by-mtime
recording and pcap (or null when nothing matches).

Pure helper — no pjsua / asyncio dependency. Tested in
`tests/scenario_engine/test_artifacts.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _latest_after(directory: Path, suffix: str, started_at: float) -> Path | None:
    """Return the file in `directory` with extension `suffix` whose mtime is
    ≥ `started_at` and is the largest among such files. None if nothing
    matches or the directory does not exist."""
    if not directory.is_dir():
        return None
    best: Path | None = None
    best_mtime: float = -1.0
    for f in directory.glob(f"*{suffix}"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < started_at:
            continue
        if st.st_mtime > best_mtime:
            best = f
            best_mtime = st.st_mtime
    return best


def _host_join(host_root: str | None, container_root: Path, container_path: Path) -> str | None:
    """Translate a container-side absolute path to its host-side equivalent.

    `container_path` lives under `container_root`; we strip the prefix and
    re-anchor at `host_root`. None when `host_root` is unset.
    """
    if host_root is None:
        return None
    try:
        rel = container_path.relative_to(container_root)
    except ValueError:
        return None
    return str(Path(host_root) / rel)


def collect_artifacts(
    phones: list[str],
    started_at: float,
    recordings_root: Path,
    captures_root: Path,
    host_recordings_root: str | None = None,
    host_captures_root: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Return per-phone artifact bundle for files created since `started_at`.

    Layout assumed:
      `<recordings_root>/<phone_id>/<stem>.wav` paired with `<stem>.meta.json`
      `<captures_root>/<phone_id>/<stem>.pcap`

    Each phone in `phones` maps to either:
      * `None` — phone produced no recordings or pcaps during this run, or
      * `dict` with keys: recording, recording_meta, pcap,
        host_recording, host_recording_meta, host_pcap.
        Any individual field may be None when its file did not appear.

    `started_at` is wall-clock epoch seconds (`time.time()`) — must match
    the timebase of `os.stat().st_mtime`. Do not use `time.monotonic()`.
    """
    result: dict[str, dict[str, Any] | None] = {}
    for phone_id in phones:
        rec_dir = recordings_root / phone_id
        cap_dir = captures_root / phone_id

        rec = _latest_after(rec_dir, ".wav", started_at)
        pcap = _latest_after(cap_dir, ".pcap", started_at)

        if rec is None and pcap is None:
            result[phone_id] = None
            continue

        rec_meta: Path | None = None
        if rec is not None:
            candidate = rec.with_suffix(".meta.json")
            if candidate.is_file():
                rec_meta = candidate

        result[phone_id] = {
            "recording": str(rec) if rec is not None else None,
            "recording_meta": str(rec_meta) if rec_meta is not None else None,
            "pcap": str(pcap) if pcap is not None else None,
            "host_recording": _host_join(
                host_recordings_root, recordings_root, rec
            ) if rec is not None else None,
            "host_recording_meta": _host_join(
                host_recordings_root, recordings_root, rec_meta
            ) if rec_meta is not None else None,
            "host_pcap": _host_join(
                host_captures_root, captures_root, pcap
            ) if pcap is not None else None,
        }
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/scenario_engine/test_artifacts.py -v
```
Expected: PASS — all 11 tests green.

- [ ] **Step 5: Commit**

```bash
git add src/scenario_engine/artifacts.py tests/scenario_engine/test_artifacts.py
git commit -m "$(cat <<'EOF'
feat(scenario): collect_artifacts() pure helper

After-run sweep of /recordings/<phone>/*.wav + /captures/<phone>/*.pcap,
filtered by mtime ≥ started_at, latest-wins per phone. Pairs each
recording with its <stem>.meta.json sidecar. Optional host-side roots
re-anchor container paths so MCP clients (Claude Code) can read the
artifacts via Read/Bash without resolving the bind mount themselves.
Phones with no fresh artifacts return None — distinguishable from
"phone had recording but pcap missing".

11 unit tests cover: empty phones, missing files, mtime filter
(at boundary), latest-wins, host-path mapping, recording-disabled
pcap-only case, missing-directory tolerance.
EOF
)"
```

---

## Task 2: Wire artifacts into ScenarioResult / ScenarioRunner

**Files:**
- Modify: `src/scenario_engine/orchestrator.py`
- Modify: `src/scenario_engine/__init__.py`
- Modify: `tests/scenario_engine/test_orchestrator.py`

- [ ] **Step 1: Write failing test**

Add to `tests/scenario_engine/test_orchestrator.py` at the bottom (before `# end of file`):

```python
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


def test_run_artifacts_default_empty_when_roots_unset(tmp_path) -> None:
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
            timeout_ms=100,
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/scenario_engine/test_orchestrator.py::test_run_attaches_artifacts_for_files_created_during_run tests/scenario_engine/test_orchestrator.py::test_run_artifacts_default_empty_when_roots_unset -v
```
Expected: FAIL — `ScenarioRunner.__init__()` does not accept `recordings_root`, `to_dict()` lacks `artifacts`.

- [ ] **Step 3: Update `ScenarioResult` dataclass + `to_dict()`**

In `src/scenario_engine/orchestrator.py`, replace the `ScenarioResult` dataclass:

```python
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
```

- [ ] **Step 4: Extend `ScenarioRunner.__init__` with artifact roots**

In `src/scenario_engine/orchestrator.py`, replace the existing `ScenarioRunner.__init__`:

```python
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
```

No new top-level import required: `from __future__ import annotations` is already at the top of the file, so the quoted `"Path | None"` annotation stays a string at runtime.

- [ ] **Step 5: Add module-level import of `collect_artifacts` and record `started_at_wall`**

In `src/scenario_engine/orchestrator.py`, add to the existing import block (alongside the other `from src.scenario_engine...` imports):

```python
from src.scenario_engine.action_executor import ActionExecutor
from src.scenario_engine.artifacts import collect_artifacts
from src.scenario_engine.event_bus import Event, EventBus
from src.scenario_engine.hook_runtime import Hook, HookRuntime, _event_matches_predicates
from src.scenario_engine.timeline import Timeline, TimelineRecorder
```

(Top-level is safe — `artifacts.py` has no dependency on `orchestrator.py`, so no circular import.)

- [ ] **Step 5b: Record `started_at_wall` at start of run()**

In `src/scenario_engine/orchestrator.py`, modify `ScenarioRunner.run()`. Right after `t_start = time.monotonic()` add:

```python
        t_start = time.monotonic()
        started_at_wall = time.time()
        errors: list[dict[str, Any]] = []
```

Then, near the end of `run()`, just BEFORE the final `return ScenarioResult(...)`, replace the existing return block:

```python
        elapsed_ms = (time.monotonic() - t_start) * 1000

        artifacts: dict[str, Any] = {}
        if self._recordings_root is not None and self._captures_root is not None and scenario.phones:
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
```

The early-return path inside the validation-error branch (around line 116) does NOT need artifacts — it never touched recordings — so leave the second `return ScenarioResult(...)` (validation-fail path) untouched.

- [ ] **Step 6: Plumb roots through `run_scenario` top-level helper**

In `src/scenario_engine/orchestrator.py`, modify `run_scenario()`:

```python
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

    `recordings_root` / `captures_root` (and their `host_*` counterparts)
    enable the post-run artifact sweep. When unset, `result.artifacts` is
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
```

- [ ] **Step 7: Export `collect_artifacts` from package**

In `src/scenario_engine/__init__.py`, replace the import block + `__all__`:

```python
"""Event-driven scenario engine for pjsua MCP."""

from __future__ import annotations

from src.scenario_engine.artifacts import collect_artifacts
from src.scenario_engine.event_bus import Event, EventBus, Subscription
from src.scenario_engine.hook_runtime import Hook, HookRuntime
from src.scenario_engine.orchestrator import Scenario, ScenarioResult, run_scenario
from src.scenario_engine.timeline import Timeline, TimelineEntry, TimelineRecorder
from src.scenario_engine.validator import KNOWN_ACTIONS, validate_scenario

__all__ = [
    "Event",
    "EventBus",
    "Hook",
    "HookRuntime",
    "KNOWN_ACTIONS",
    "Scenario",
    "ScenarioResult",
    "Subscription",
    "Timeline",
    "TimelineEntry",
    "TimelineRecorder",
    "collect_artifacts",
    "run_scenario",
    "validate_scenario",
]
```

- [ ] **Step 8: Run new tests to verify they pass**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/scenario_engine/test_orchestrator.py -v
```
Expected: PASS — including the two new tests.

- [ ] **Step 9: Run full scenario_engine test suite to verify no regressions**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/scenario_engine/ -v
```
Expected: PASS — all existing scenario_engine tests still green.

- [ ] **Step 10: Commit**

```bash
git add src/scenario_engine/orchestrator.py src/scenario_engine/__init__.py tests/scenario_engine/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(scenario): ScenarioResult.artifacts collected post-stop

ScenarioRunner now records started_at_wall (time.time()) at run start
and, when given recordings_root + captures_root, sweeps each phone's
freshly-created WAV/pcap into ScenarioResult.artifacts via the pure
helper added previously. Roots default to None — older callers (engine
tests, unit suites) see artifacts = {} and stay green.

Backward-compatible: existing fields (status, elapsed_ms, timeline,
errors, reason) unchanged. New `artifacts` key is ALWAYS present in
to_dict() — empty dict when not collected, populated otherwise.
EOF
)"
```

---

## Task 3: Server wiring (env vars + production roots)

**Files:**
- Modify: `src/server.py`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

- [ ] **Step 1: Add roots-from-env to `server.py`**

In `src/server.py`, after the existing module-level constants `_RECORDINGS_ROOT = Path("/recordings")` / `_CAPTURES_ROOT = Path("/captures")` (around line 863-864), add:

```python
_RECORDINGS_ROOT = Path("/recordings")
_CAPTURES_ROOT = Path("/captures")

# Host-side bind targets — set by docker-compose so MCP clients (Claude
# Code, which runs OUTSIDE the container) can read artifacts via host
# paths without resolving the mount themselves. Unset → host_* fields
# in run_scenario.artifacts come back as None.
_HOST_RECORDINGS_ROOT = os.environ.get("PJSUA_MCP_HOST_RECORDINGS_DIR") or None
_HOST_CAPTURES_ROOT = os.environ.get("PJSUA_MCP_HOST_CAPTURES_DIR") or None
```

(`os` is already imported at top of file.)

- [ ] **Step 2: Pass roots to `run_scenario_impl`**

In `src/server.py`, replace the `run_scenario_impl(...)` call inside the `run_scenario` MCP tool:

```python
        result = await run_scenario_impl(
            scn_input,
            bus=event_bus,
            call_manager=call_mgr,
            registry=registry,
            loop=loop,
            engine=engine,
            recordings_root=_RECORDINGS_ROOT,
            captures_root=_CAPTURES_ROOT,
            host_recordings_root=_HOST_RECORDINGS_ROOT,
            host_captures_root=_HOST_CAPTURES_ROOT,
        )
```

- [ ] **Step 3: Update `docker-compose.yml`**

Replace the file body of `docker-compose.yml`:

```yaml
services:
  pjsua-mcp:
    build: .
    # Run as the host user so files on bind mounts (./captures, ./recordings)
    # are owned by the invoking user, not root. Populate UID/GID via .env
    # (see .env.example) — docker-compose reads it automatically. Fallback
    # is 1000:1000 if the env is absent.
    user: "${UID:-1000}:${GID:-1000}"
    network_mode: host
    stdin_open: true
    cap_add:
      - NET_RAW
      - NET_ADMIN
    environment:
      # Host-side absolute paths of the bind targets below — let
      # `run_scenario` return host paths so MCP clients (which run
      # outside the container) can Read/Bash the artifacts directly.
      # PJSUA_MCP_HOST_RECORDINGS_DIR / _CAPTURES_DIR fall through from
      # .env (or wherever the caller sets them).
      - PJSUA_MCP_HOST_RECORDINGS_DIR=${PJSUA_MCP_HOST_RECORDINGS_DIR:-}
      - PJSUA_MCP_HOST_CAPTURES_DIR=${PJSUA_MCP_HOST_CAPTURES_DIR:-}
    volumes:
      - ./captures:/captures
      - ./recordings:/recordings
      - ./config:/config:ro
```

- [ ] **Step 4: Update `.env.example`**

Append at the end of `.env.example`:

```
# --- Scenario artifact host paths ---
# When set, run_scenario.artifacts.<phone>.host_recording / host_pcap come
# back as absolute host-side paths (otherwise null). For the bare repo,
# point these at the absolute path of ./recordings and ./captures relative
# to where you run `docker compose up`. Plugin users get this auto-populated
# by their bootstrap (no manual edit needed). Leave blank if you don't need
# host paths in scenario results.

PJSUA_MCP_HOST_RECORDINGS_DIR=
PJSUA_MCP_HOST_CAPTURES_DIR=
```

- [ ] **Step 5: Sanity check — server still imports cleanly**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner python -c "import src.server; print('ok')"
```
Expected: PASS — prints `ok`.

- [ ] **Step 6: Commit**

```bash
git add src/server.py docker-compose.yml .env.example
git commit -m "$(cat <<'EOF'
feat(server): wire run_scenario artifact-collection knobs

run_scenario MCP tool now passes /recordings + /captures (and the
optional PJSUA_MCP_HOST_RECORDINGS_DIR / _CAPTURES_DIR env vars) into
the orchestrator so the result's `artifacts` field is populated.

docker-compose passes both env vars through (default empty). For
plugin deployments, the plugin's docker-compose should set them to
${CLAUDE_PLUGIN_DATA}/recordings and /captures respectively — see
sip-dialer plugin's compose file.

When the env vars are blank, container paths still come back; only
the host_* mirror fields are null.
EOF
)"
```

---

## Task 4: Integration test (live Asterisk + MCP)

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_integration.py` at the bottom:

```python
class TestRunScenarioArtifacts:
    """proposal-04 acceptance: run_scenario.artifacts isolates the current
    run's recordings/pcaps using mtime, latest-wins per phone.

    Skipped without SIP_DOMAIN. Two sequential runs verify that the second
    run reports a *different* recording — not the first run's leftover.
    """

    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            _add_phone(
                self.client, "a", SIP_USER_A, SIP_PASS_A,
                recording_enabled=True, capture_enabled=True,
            )
            _add_phone(
                self.client, "b", SIP_USER_B, SIP_PASS_B,
                auto_answer=True,
                recording_enabled=True, capture_enabled=True,
            )
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            yield

    def _run_scenario_basic_call(self) -> dict:
        scenario = {
            "name": "basic-call-roundtrip",
            "phones": ["a", "b"],
            "initial_actions": [
                {"action": "make_call", "phone_id": "a",
                 "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}"},
            ],
            "hooks": [
                {
                    "when": "call.state.confirmed",
                    "on_phone": "a",
                    "once": True,
                    "then": [{"wait": "1500ms"}, {"action": "hangup", "phone_id": "a"}],
                },
            ],
            "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
            "timeout_ms": 12000,
        }
        return _parse_tool_result(
            self.client.call_tool("run_scenario", {"scenario": scenario})
        )

    @skip_no_domain
    def test_artifacts_present_for_both_phones(self):
        from pathlib import Path

        result = self._run_scenario_basic_call()
        assert result["status"] == "ok", f"first run failed: {result}"
        assert "artifacts" in result, "result missing 'artifacts' field"
        a = result["artifacts"]["a"]
        b = result["artifacts"]["b"]
        assert a is not None, f"alice has no artifacts; got {result['artifacts']}"
        assert b is not None, f"bob has no artifacts; got {result['artifacts']}"
        assert Path(a["recording"]).is_file(), (
            f"recording for alice is not a real file: {a['recording']}"
        )
        assert a["recording"].endswith(".wav")
        # capture_enabled=True on alice → pcap should be set.
        # Tolerant check — tcpdump may have failed to bind in the test
        # container, in which case pcap can be missing. Don't hard-fail.
        if a["pcap"] is not None:
            assert a["pcap"].endswith(".pcap")
        # Sidecar meta.json may not be flushed by the time the scenario
        # stops, but if present it must be a valid JSON file.
        if a["recording_meta"] is not None:
            import json
            json.loads(Path(a["recording_meta"]).read_text())

    @skip_no_domain
    def test_two_sequential_runs_report_different_recordings(self):
        """Acceptance #1: artifacts for run-2 must point at run-2's files,
        not run-1's leftovers."""
        first = self._run_scenario_basic_call()
        assert first["status"] == "ok"
        first_rec_a = first["artifacts"]["a"]["recording"]

        # Brief pause so file mtimes are distinguishable across runs even
        # on filesystems with low resolution.
        time.sleep(1.2)

        second = self._run_scenario_basic_call()
        assert second["status"] == "ok"
        second_rec_a = second["artifacts"]["a"]["recording"]

        assert first_rec_a != second_rec_a, (
            f"run-2 reported run-1's recording — mtime filter not isolating: "
            f"first={first_rec_a} second={second_rec_a}"
        )

    @skip_no_domain
    def test_phone_not_in_call_returns_null(self):
        """Acceptance #3: phone listed in `phones` but with no calls during
        this run → artifacts[phone] == None."""
        # carol is NOT registered — engine will reject a make_call on it,
        # but here we just want a phone that produces no recordings/pcaps
        # during the scenario. Use 'b' but in a scenario that doesn't
        # involve b — actually since auto_answer=True and the dialer goes
        # to b, b will produce artifacts too. Easier: include a phantom
        # phone_id that has no underlying registration.
        #
        # Add a third phone but let it idle.
        _add_phone(
            self.client, "c", SIP_USER_C, SIP_PASS_C,
            recording_enabled=True, capture_enabled=True,
        )
        _wait_phone_registered(self.client, "c")

        scenario = {
            "name": "c-idle",
            "phones": ["a", "b", "c"],   # c listed but not engaged
            "initial_actions": [
                {"action": "make_call", "phone_id": "a",
                 "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}"},
            ],
            "hooks": [
                {
                    "when": "call.state.confirmed",
                    "on_phone": "a",
                    "once": True,
                    "then": [{"wait": "800ms"}, {"action": "hangup", "phone_id": "a"}],
                },
            ],
            "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
            "timeout_ms": 8000,
        }
        result = _parse_tool_result(
            self.client.call_tool("run_scenario", {"scenario": scenario})
        )
        assert result["status"] == "ok", f"scenario failed: {result}"
        assert result["artifacts"]["c"] is None, (
            f"c should be null (idle), got {result['artifacts']['c']}"
        )
```

- [ ] **Step 2: Run integration tests inside docker-compose.test.yml**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/test_integration.py::TestRunScenarioArtifacts -v
```
Expected: PASS — all 3 tests green. (`time.sleep(1.2)` between runs ensures mtimes differ.)

- [ ] **Step 3: Run the full integration suite to confirm no regressions elsewhere**

Run:
```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/test_integration.py -v -x
```
Expected: PASS for new tests; existing TestRecordingToggleLive / TestCaptureLive / TestCallFlow still green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "$(cat <<'EOF'
test(integration): TestRunScenarioArtifacts (proposal-04 acceptance)

Three live tests covering the new run_scenario.artifacts field:
1) both phones in a basic-call roundtrip get a populated artifact dict
   with a real WAV file path;
2) two sequential runs report different recordings — verifies the
   mtime ≥ started_at filter actually isolates THIS run from history;
3) a phone listed in `phones` but never engaged comes back as null
   (not error, not undefined).

Skipped without SIP_DOMAIN. Tolerant on optional pcap/meta fields when
tcpdump/meta sidecar timing varies in container environments.
EOF
)"
```

---

## Task 5: CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add Unreleased entry to CHANGELOG.md**

In `CHANGELOG.md`, under the existing `## [Unreleased]` → `### Added` block, prepend:

```markdown
- `run_scenario` result now includes an `artifacts: {phone_id: {...}}` dict
  populated post-stop. Each phone in `scenario.phones` maps to either
  `null` (no recording/pcap created during this run) or a dict with
  `recording`, `recording_meta`, `pcap` (container paths) and
  `host_recording`, `host_recording_meta`, `host_pcap` (host-side paths
  when `PJSUA_MCP_HOST_RECORDINGS_DIR` / `PJSUA_MCP_HOST_CAPTURES_DIR`
  are set in the env, otherwise `null`). Files are filtered by
  `mtime ≥ scenario.started_at` so a run never picks up a previous
  run's artifacts; latest-mtime wins per phone. Backward-compatible —
  existing fields (`status`, `elapsed_ms`, `timeline`, `errors`,
  `reason`) unchanged.
- `src/scenario_engine/artifacts.py:collect_artifacts(...)` — pure
  helper backing the above. 11 unit tests in
  `tests/scenario_engine/test_artifacts.py` cover mtime filter,
  latest-wins, pair-with-meta-sidecar, host path mapping, missing-
  directory tolerance, recording-disabled-pcap-only edge case.
- Integration tests `TestRunScenarioArtifacts` in
  `tests/test_integration.py` covering proposal-04 acceptance criteria
  against live Asterisk: artifacts populated for both legs of a
  roundtrip, two sequential runs report distinct recordings,
  unengaged phones come back as null.
```

In `CHANGELOG.md`, also under `## [Unreleased]`, add a new `### Configuration` section right after `### Added`:

```markdown
### Configuration
- New env vars `PJSUA_MCP_HOST_RECORDINGS_DIR` and
  `PJSUA_MCP_HOST_CAPTURES_DIR` (both optional). When set, the host-
  side absolute paths of the `/recordings` and `/captures` bind targets
  are surfaced in `run_scenario.artifacts.<phone>.host_*` fields so
  out-of-container clients (Claude Code, plugin host) can Read/Bash
  artifacts without resolving the bind mount themselves. Empty / unset
  → `host_*` fields come back as `null`. `docker-compose.yml` passes
  both through; `.env.example` documents the wiring.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: CHANGELOG entry for run_scenario artifacts (proposal-04)"
```

---

## Final verification

After all five tasks land, do one full sweep:

- [ ] **Run scenario_engine unit tests:**

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/scenario_engine/ -v
```

- [ ] **Run scenario_engine + non-integration tests:**

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner pytest tests/ -v -m "not integration"
```

- [ ] **Run full integration suite (Asterisk + MCP):**

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner
```

Expected: All green.

- [ ] **Smoke check `git log` shows 5 logical commits:**

```bash
git log --oneline -5
```
Expected (newest at top):
1. docs: CHANGELOG entry for run_scenario artifacts (proposal-04)
2. test(integration): TestRunScenarioArtifacts ...
3. feat(server): wire run_scenario artifact-collection knobs
4. feat(scenario): ScenarioResult.artifacts collected post-stop
5. feat(scenario): collect_artifacts() pure helper

---

## Self-Review checklist

- ✅ Spec coverage: all 5 acceptance criteria mapped to tasks (see top of plan).
- ✅ Placeholder scan: no TBD / TODO; every step has full code or exact commands.
- ✅ Type consistency: `collect_artifacts(...)` signature consistent across artifacts.py, orchestrator.py, server.py; field names (`recording`, `recording_meta`, `pcap`, `host_*`) consistent across producer + tests.
- ✅ Plugin docker-compose update is OUT OF SCOPE for this MCP-side plan — noted in Task 3 commit message but not enforced here. Plugin lives in `portaone-plugins` and gets a separate change.
