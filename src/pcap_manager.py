"""Packet capture management via tcpdump subprocess."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CAPTURES_DIR = Path("/captures")


class PcapManager:
    """Manages tcpdump subprocess for packet capture."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._current_file: Path | None = None

    async def start(
        self,
        interface: str = "any",
        port: int | None = None,
    ) -> dict[str, Any]:
        """Start tcpdump capture."""
        if self._process is not None:
            raise RuntimeError("Capture already running — stop it first")

        CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"capture_{timestamp}.pcap"
        filepath = CAPTURES_DIR / filename
        self._current_file = filepath

        cmd = [
            "tcpdump",
            "-i", interface,
            "-w", str(filepath),
            "-U",  # packet-buffered output
        ]

        if port:
            cmd.extend(["port", str(port)])

        log.info("Starting tcpdump: %s", " ".join(cmd))
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        return {
            "filename": filename,
            "pid": self._process.pid,
        }

    async def stop(self) -> dict[str, Any]:
        """Stop tcpdump capture and return file info."""
        if self._process is None:
            raise RuntimeError("No capture running")

        self._process.terminate()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()

        self._process = None

        file_size = 0
        filename = ""
        if self._current_file and self._current_file.exists():
            file_size = self._current_file.stat().st_size
            filename = self._current_file.name

        return {
            "filename": filename,
            "file_size": file_size,
        }

    def get_pcap_info(self, filename: str | None = None) -> dict[str, Any]:
        """Get info about a pcap file."""
        if filename:
            filepath = CAPTURES_DIR / filename
        elif self._current_file:
            filepath = self._current_file
        else:
            # Find most recent pcap
            pcaps = sorted(CAPTURES_DIR.glob("*.pcap"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not pcaps:
                raise RuntimeError("No capture files found")
            filepath = pcaps[0]

        if not filepath.exists():
            raise RuntimeError(f"File not found: {filepath.name}")

        return {
            "filename": filepath.name,
            "file_size": filepath.stat().st_size,
            "file_path": str(filepath),
        }

    async def cleanup(self) -> None:
        """Stop any running capture (shutdown hook)."""
        if self._process:
            try:
                await self.stop()
            except Exception:
                log.exception("Error stopping capture during cleanup")
