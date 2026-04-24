"""Packet capture management via tcpdump subprocess."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CAPTURES_ROOT = Path("/captures")
CAPTURES_DIR = CAPTURES_ROOT  # legacy alias for host-wide captures


@dataclass
class PhoneCapture:
    """State of one per-phone auto-capture subprocess."""

    process: asyncio.subprocess.Process
    file: Path
    call_id: int | None
    started_at: str


class PcapManager:
    """Manages tcpdump subprocesses for packet capture.

    Two modes coexist:

    - Legacy host-wide single capture (start/stop) — one global subprocess
      triggered via the `start_capture` tool.
    - Per-phone auto-capture (start_for_phone/stop_for_phone) — one
      subprocess per phone, keyed in `_phone_processes`. Driven by the
      `capture_enabled` flag on `PhoneConfig`: the first CONFIRMED call
      on a phone opens the capture, the last DISCONNECTED closes it.

    Filename layout:
      - host-wide (no phone_id):        /captures/capture_<ts>.pcap
      - per-phone with active call:     /captures/<phone_id>/call_<call_id>_<ts>.pcap
      - per-phone without active call:  /captures/<phone_id>/capture_<ts>.pcap
    """

    def __init__(self) -> None:
        # Legacy host-wide capture
        self._process: asyncio.subprocess.Process | None = None
        self._current_file: Path | None = None
        self._current_phone_id: str | None = None
        self._current_call_id: int | None = None
        # Per-phone auto-captures
        self._phone_processes: dict[str, PhoneCapture] = {}

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

    # ------------------------------------------------------------------
    # Per-phone auto-capture
    # ------------------------------------------------------------------
    async def start_for_phone(
        self,
        phone_id: str,
        call_id: int | None,
    ) -> dict[str, Any]:
        """Start an auto-capture tcpdump subprocess dedicated to `phone_id`.

        A broad UDP BPF filter is used so that a re-INVITE with a new RTP
        port does not drop packets mid-call. Disk overhead is the price —
        rotate or stop explicitly if the stand is noisy.

        Raises RuntimeError if this phone already has an active capture.
        """
        if phone_id in self._phone_processes:
            raise RuntimeError(f"Phone {phone_id!r} already capturing")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        out_dir = CAPTURES_ROOT / phone_id
        out_dir.mkdir(parents=True, exist_ok=True)
        if call_id is not None:
            filename = f"call_{call_id}_{timestamp}.pcap"
        else:
            filename = f"capture_{timestamp}.pcap"
        filepath = out_dir / filename

        cmd = [
            "tcpdump",
            "-i", "any",
            "-w", str(filepath),
            "-U",        # packet-buffered output
            "udp",       # broad filter — survives re-INVITE RTP port changes
        ]

        log.info("[%s] Starting auto-capture: %s", phone_id, " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._phone_processes[phone_id] = PhoneCapture(
            process=proc,
            file=filepath,
            call_id=call_id,
            started_at=timestamp,
        )
        return {
            "phone_id": phone_id,
            "filename": filename,
            "pid": proc.pid,
        }

    async def stop_for_phone(self, phone_id: str) -> dict[str, Any]:
        """Stop the auto-capture subprocess for `phone_id`.

        Idempotent — returns `{"status": "not_running"}` if nothing is
        active for this phone. Returns the filename and byte-count so
        callers can verify the pcap was flushed.
        """
        cap = self._phone_processes.pop(phone_id, None)
        if cap is None:
            return {"phone_id": phone_id, "status": "not_running"}

        cap.process.terminate()
        try:
            await asyncio.wait_for(cap.process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            cap.process.kill()
            await cap.process.wait()

        file_size = cap.file.stat().st_size if cap.file.exists() else 0
        return {
            "phone_id": phone_id,
            "filename": cap.file.name,
            "file_size": file_size,
        }

    def is_phone_capturing(self, phone_id: str) -> bool:
        """True when an auto-capture subprocess is live for `phone_id`."""
        return phone_id in self._phone_processes

    def current_pcap_path_for(self, phone_id: str) -> str | None:
        """Return the active pcap path for `phone_id`, or None.

        Checked in order: per-phone auto-capture first, then the legacy
        host-wide capture (if it was started for this phone_id). Used by
        `SipCall._write_meta_sidecar` so the recording's meta.json can
        point at the paired pcap.
        """
        cap = self._phone_processes.get(phone_id)
        if cap is not None:
            return str(cap.file)
        if (
            self._process is not None
            and self._current_file is not None
            and self._current_phone_id == phone_id
        ):
            return str(self._current_file)
        return None

    async def cleanup(self) -> None:
        """Stop every running capture (shutdown hook)."""
        for phone_id in list(self._phone_processes.keys()):
            try:
                await self.stop_for_phone(phone_id)
            except Exception:
                log.exception("cleanup: stop_for_phone(%s) failed", phone_id)
        if self._process:
            try:
                await self.stop()
            except Exception:
                log.exception("Error stopping capture during cleanup")
