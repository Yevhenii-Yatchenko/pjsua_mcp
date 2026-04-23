"""Packet capture management via tcpdump subprocess."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CAPTURES_ROOT = Path("/captures")
CAPTURES_DIR = CAPTURES_ROOT  # legacy alias for host-wide captures


class PcapManager:
    """Manages tcpdump subprocess for packet capture.

    Filename layout:
      - host-wide (no phone_id):        /captures/capture_<ts>.pcap
      - per-phone with active call:     /captures/<phone_id>/call_<call_id>_<ts>.pcap
      - per-phone without active call:  /captures/<phone_id>/capture_<ts>.pcap

    The per-phone+call variant uses the same basename as the phone's
    concurrent recording, so `call_<N>_<ts>.wav` and `.pcap` pair up
    without any timestamp matching.
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._current_file: Path | None = None
        self._current_phone_id: str | None = None
        self._current_call_id: int | None = None

    async def start(
        self,
        interface: str = "any",
        port: int | None = None,
        phone_id: str | None = None,
        call_id: int | None = None,
    ) -> dict[str, Any]:
        """Start tcpdump capture.

        If `phone_id` is given, the pcap lands under `/captures/<phone_id>/`.
        If `call_id` is also given (caller resolves it from CallManager),
        the filename matches the recording of that call.
        """
        if self._process is not None:
            raise RuntimeError("Capture already running — stop it first")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if phone_id is not None:
            out_dir = CAPTURES_ROOT / phone_id
            if call_id is not None:
                filename = f"call_{call_id}_{timestamp}.pcap"
            else:
                filename = f"capture_{timestamp}.pcap"
        else:
            out_dir = CAPTURES_ROOT
            filename = f"capture_{timestamp}.pcap"

        out_dir.mkdir(parents=True, exist_ok=True)
        filepath = out_dir / filename
        self._current_file = filepath
        self._current_phone_id = phone_id
        self._current_call_id = call_id

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
        phone_id = self._current_phone_id
        self._current_phone_id = None
        self._current_call_id = None

        file_size = 0
        filename = ""
        if self._current_file and self._current_file.exists():
            file_size = self._current_file.stat().st_size
            filename = self._current_file.name

        return {
            "filename": filename,
            "file_size": file_size,
            "phone_id": phone_id,
        }

    def get_pcap_info(self, filename: str | None = None) -> dict[str, Any]:
        """Get info about a pcap file (defaults to the most recent capture)."""
        if filename:
            # Allow either bare name or a path fragment (e.g. "a/call_0_...pcap").
            candidate = CAPTURES_ROOT / filename
            if candidate.exists():
                filepath = candidate
            else:
                # Fallback: walk the tree to find a matching basename.
                matches = list(CAPTURES_ROOT.rglob(Path(filename).name))
                if not matches:
                    raise RuntimeError(f"File not found: {filename}")
                filepath = matches[0]
        elif self._current_file:
            filepath = self._current_file
        else:
            # Find the most recent pcap across the whole tree.
            pcaps = sorted(
                CAPTURES_ROOT.rglob("*.pcap"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
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

    def current_pcap_path_for(self, phone_id: str) -> str | None:
        """Return the active pcap path for `phone_id`, or None.

        Used when writing a recording's `.meta.json` sidecar so the meta
        can point at the paired pcap. Only returns a path when a capture
        is running *and* it was started for this phone_id.
        """
        if (
            self._process is None
            or self._current_file is None
            or self._current_phone_id != phone_id
        ):
            return None
        return str(self._current_file)

    async def cleanup(self) -> None:
        """Stop any running capture (shutdown hook)."""
        if self._process:
            try:
                await self.stop()
            except Exception:
                log.exception("Error stopping capture during cleanup")
