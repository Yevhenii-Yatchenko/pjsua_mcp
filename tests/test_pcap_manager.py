"""Unit tests for PcapManager — file lookup and guards."""

from __future__ import annotations

import time

import pytest

from src.pcap_manager import PcapManager


class TestGetPcapInfo:
    def test_specific_file(self, tmp_captures_dir):
        (tmp_captures_dir / "test.pcap").write_bytes(b"\x00" * 100)
        mgr = PcapManager()
        info = mgr.get_pcap_info(filename="test.pcap")
        assert info["filename"] == "test.pcap"
        assert info["file_size"] == 100

    def test_most_recent(self, tmp_captures_dir):
        (tmp_captures_dir / "old.pcap").write_bytes(b"\x00" * 10)
        time.sleep(0.05)  # ensure different mtime
        (tmp_captures_dir / "new.pcap").write_bytes(b"\x00" * 20)
        mgr = PcapManager()
        info = mgr.get_pcap_info()
        assert info["filename"] == "new.pcap"

    def test_missing_file(self, tmp_captures_dir):
        mgr = PcapManager()
        with pytest.raises(RuntimeError, match="not found"):
            mgr.get_pcap_info(filename="nonexistent.pcap")

    def test_no_files(self, tmp_captures_dir):
        mgr = PcapManager()
        with pytest.raises(RuntimeError, match="No capture files"):
            mgr.get_pcap_info()


class TestStopGuard:
    def test_stop_before_start(self):
        mgr = PcapManager()
        with pytest.raises(RuntimeError, match="No capture running"):
            import asyncio
            asyncio.run(mgr.stop())
