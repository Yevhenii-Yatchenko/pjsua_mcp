"""Per-phone artifact collection for scenario runs.

After a scenario stops, sweep `<recordings_root>/<phone_id>/*.wav` and
`<captures_root>/<phone_id>/*.pcap` for files created during this run
(mtime ≥ scenario.started_at). Each phone reports the latest-by-mtime
recording and pcap (or null when nothing matches).

All path strings in the result are translated to **host-side** absolute
paths (when `host_recordings_root` / `host_captures_root` are provided)
so out-of-container MCP clients can read them via Read/Bash directly.
When the host roots are unset, container paths are returned unchanged
as a graceful fallback.

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


def external_path(
    container_path: str | Path | None,
    container_root: Path,
    host_root: str | None,
) -> str | None:
    """Re-anchor a container path under `host_root` for MCP output.

    The MCP server runs inside a Docker container; its clients (Claude
    Code, plugin host) usually run OUTSIDE. Container paths like
    `/recordings/alice/x.wav` cannot be opened from the host, so every
    file path in an MCP response should be translated through this
    helper.

    * `container_path is None` → returns None (path-not-set passes through).
    * `host_root is None`     → returns the container path unchanged
      (graceful fallback so the MCP still works without env wiring; the
      result just won't be openable from outside the container).
    * `container_path` not under `container_root` → also returns the
      container path unchanged (caller bug — better to surface the raw
      path than mangle it).
    * Otherwise: `<host_root>/<container_path - container_root>`.
    """
    if container_path is None:
        return None
    p = container_path if isinstance(container_path, Path) else Path(container_path)
    if host_root is None:
        return str(p)
    try:
        rel = p.relative_to(container_root)
    except ValueError:
        return str(p)
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
      * `dict` with keys: `recording`, `recording_meta`, `pcap`. Path
        values are host-side strings (when host roots are set) or fall
        back to container paths. Any field may be None if its file did
        not appear (recording_enabled vs capture_enabled are independent
        knobs).

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
            "recording": external_path(rec, recordings_root, host_recordings_root),
            "recording_meta": external_path(rec_meta, recordings_root, host_recordings_root),
            "pcap": external_path(pcap, captures_root, host_captures_root),
        }
    return result
