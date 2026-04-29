"""Unit tests for PcapManager — per-phone auto-capture."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from src.pcap_manager import PcapManager


# ---------------------------------------------------------------------------
# Per-phone auto-capture — exercises start_for_phone / stop_for_phone without
# actually shelling out to tcpdump. A dummy PhoneCapture replaces the real
# subprocess dict entry so the test asserts against the state machine.
# ---------------------------------------------------------------------------

@dataclass
class _FakeProc:
    """Stand-in for asyncio.subprocess.Process in unit tests."""

    pid: int = 12345
    _terminated: bool = False
    _killed: bool = False

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._killed = True

    async def wait(self) -> int:
        return 0


class TestPhoneCapture:
    def test_start_for_phone_creates_per_phone_subdir(self, tmp_captures_dir, monkeypatch):
        """start_for_phone writes pcap under /captures/<phone_id>/ and registers state."""
        from src.pcap_manager import PhoneCapture
        import src.pcap_manager as pm

        # Substitute a fake subprocess so we don't actually exec tcpdump.
        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", _fake_exec)

        mgr = PcapManager()
        info = asyncio.run(mgr.start_for_phone("a", call_id=7))

        assert info["phone_id"] == "a"
        assert info["filename"].startswith("call_7_")
        assert info["filename"].endswith(".pcap")
        assert mgr.is_phone_capturing("a")

        # The pcap file path lives under the per-phone subdir.
        cap = mgr._phone_processes["a"]
        assert cap.file.parent == tmp_captures_dir / "a"
        assert cap.call_id == 7

    def test_start_for_phone_no_call_id_uses_capture_prefix(self, tmp_captures_dir, monkeypatch):
        """Without call_id the filename uses the capture_ prefix, not call_ prefix."""
        import src.pcap_manager as pm

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", _fake_exec)

        mgr = PcapManager()
        info = asyncio.run(mgr.start_for_phone("b", call_id=None))
        assert info["filename"].startswith("capture_")
        assert not info["filename"].startswith("call_")

    def test_start_for_phone_rejects_duplicate(self, tmp_captures_dir, monkeypatch):
        """A second start_for_phone on the same phone raises — one process per phone."""
        import src.pcap_manager as pm

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", _fake_exec)

        mgr = PcapManager()
        asyncio.run(mgr.start_for_phone("a", call_id=1))

        with pytest.raises(RuntimeError, match="already capturing"):
            asyncio.run(mgr.start_for_phone("a", call_id=2))

    def test_stop_for_phone_terminates_process(self, tmp_captures_dir, monkeypatch):
        """stop_for_phone terminates the subprocess and returns flushed pcap info."""
        import src.pcap_manager as pm

        fake = _FakeProc()

        async def _fake_exec(*args, **kwargs):
            return fake

        monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", _fake_exec)

        mgr = PcapManager()
        asyncio.run(mgr.start_for_phone("a", call_id=1))
        # Pretend tcpdump wrote something to the pcap before we stopped it.
        mgr._phone_processes["a"].file.write_bytes(b"\x00" * 42)

        info = asyncio.run(mgr.stop_for_phone("a"))
        assert info["phone_id"] == "a"
        assert info["file_size"] == 42
        assert info["filename"].endswith(".pcap")
        assert fake._terminated
        assert not mgr.is_phone_capturing("a")

    def test_stop_for_phone_noop_when_idle(self):
        """stop_for_phone with no active capture returns status=not_running."""
        mgr = PcapManager()
        info = asyncio.run(mgr.stop_for_phone("nobody"))
        assert info["status"] == "not_running"
        assert info["phone_id"] == "nobody"

    def test_current_pcap_path_for_reports_per_phone(self, tmp_captures_dir, monkeypatch):
        """current_pcap_path_for returns the active per-phone pcap path so
        the recording sidecar can point at the right pcap."""
        import src.pcap_manager as pm

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", _fake_exec)

        mgr = PcapManager()
        # No capture yet.
        assert mgr.current_pcap_path_for("a") is None

        asyncio.run(mgr.start_for_phone("a", call_id=3))
        path = mgr.current_pcap_path_for("a")
        assert path is not None
        assert path.startswith(str(tmp_captures_dir / "a"))
        assert path.endswith(".pcap")

        # Unknown phone still returns None.
        assert mgr.current_pcap_path_for("nobody") is None

    def test_cleanup_stops_all_phone_processes(self, tmp_captures_dir, monkeypatch):
        """cleanup stops every per-phone capture."""
        import src.pcap_manager as pm

        procs = []

        async def _fake_exec(*args, **kwargs):
            p = _FakeProc()
            procs.append(p)
            return p

        monkeypatch.setattr(pm.asyncio, "create_subprocess_exec", _fake_exec)

        mgr = PcapManager()
        asyncio.run(mgr.start_for_phone("a", call_id=1))
        asyncio.run(mgr.start_for_phone("b", call_id=2))
        assert mgr.is_phone_capturing("a")
        assert mgr.is_phone_capturing("b")

        asyncio.run(mgr.cleanup())
        assert not mgr.is_phone_capturing("a")
        assert not mgr.is_phone_capturing("b")
        assert all(p._terminated for p in procs)
