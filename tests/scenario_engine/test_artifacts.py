"""Unit tests for src.scenario_engine.artifacts.{collect_artifacts, external_path}."""

from __future__ import annotations

import os
import time
from pathlib import Path

from src.scenario_engine.artifacts import collect_artifacts, external_path


def _touch(path: Path, mtime: float | None = None, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# external_path()
# ---------------------------------------------------------------------------

def test_external_path_none_input_passes_through():
    assert external_path(None, Path("/recordings"), "/host/data") is None
    assert external_path(None, Path("/recordings"), None) is None


def test_external_path_host_root_unset_returns_container_path():
    """Graceful fallback: env not wired → MCP still returns a usable string,
    just one that's only readable from inside the container."""
    out = external_path(
        Path("/recordings/alice/call_0.wav"),
        Path("/recordings"),
        None,
    )
    assert out == "/recordings/alice/call_0.wav"


def test_external_path_strips_root_and_anchors_host():
    out = external_path(
        Path("/recordings/alice/call_0.wav"),
        Path("/recordings"),
        "/host/data/recordings",
    )
    assert out == "/host/data/recordings/alice/call_0.wav"


def test_external_path_accepts_string_input():
    out = external_path(
        "/captures/bob/call_42.pcap",
        Path("/captures"),
        "/host/data/captures",
    )
    assert out == "/host/data/captures/bob/call_42.pcap"


def test_external_path_punts_when_container_path_outside_root():
    """Caller bug — path not under container_root. Surface the raw path
    instead of mangling it to a nonsense host path."""
    out = external_path(
        Path("/captures/alice/x.pcap"),
        Path("/recordings"),  # wrong root
        "/host/data",
    )
    assert out == "/captures/alice/x.pcap"


# ---------------------------------------------------------------------------
# collect_artifacts()
# ---------------------------------------------------------------------------

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
    # No host_root → container paths returned unchanged.
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


def test_paths_are_host_anchored_when_host_roots_provided(tmp_path):
    """Host roots set → result paths are absolute host-side strings; no
    container path leaks into the response."""
    rec = tmp_path / "recordings"
    cap = tmp_path / "captures"
    started = 1700000000.0
    _touch(rec / "alice" / "call_0.wav", mtime=started + 5)
    _touch(rec / "alice" / "call_0.meta.json", mtime=started + 5)
    _touch(cap / "alice" / "call_0.pcap", mtime=started + 5)

    out = collect_artifacts(
        phones=["alice"],
        started_at=started,
        recordings_root=rec,
        captures_root=cap,
        host_recordings_root="/host/data/recordings",
        host_captures_root="/host/data/captures",
    )
    a = out["alice"]
    assert a["recording"] == "/host/data/recordings/alice/call_0.wav"
    assert a["recording_meta"] == "/host/data/recordings/alice/call_0.meta.json"
    assert a["pcap"] == "/host/data/captures/alice/call_0.pcap"
    # No host_* sibling fields — container/host path lives in one slot.
    assert "host_recording" not in a
    assert "host_pcap" not in a


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
