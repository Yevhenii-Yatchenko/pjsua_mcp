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


def _host_join(
    host_root: str | None,
    container_root: Path,
    container_path: Path,
) -> str | None:
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
