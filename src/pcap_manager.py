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


@dataclass
class PhoneCapture:
    """State of one per-phone auto-capture subprocess."""

    process: asyncio.subprocess.Process
    file: Path
    call_id: int | None
    started_at: str


class PcapManager:
    """Manages per-phone tcpdump subprocesses for packet capture.

    One subprocess per phone, keyed in `_phone_processes`. Driven by the
    `capture_enabled` flag on `PhoneConfig`: the first audio-active call
    on a phone opens the capture, the last DISCONNECTED closes it.

    Filename layout:
      - per-phone with active call:     /captures/<phone_id>/call_<call_id>_<ts>.pcap
      - per-phone without active call:  /captures/<phone_id>/capture_<ts>.pcap
    """

    def __init__(self) -> None:
        self._phone_processes: dict[str, PhoneCapture] = {}

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
        """Return the active per-phone pcap path for `phone_id`, or None.

        Used by `SipCall._write_meta_sidecar` so the recording's
        meta.json can point at the paired pcap.
        """
        cap = self._phone_processes.get(phone_id)
        if cap is not None:
            return str(cap.file)
        return None

    async def cleanup(self) -> None:
        """Stop every running per-phone capture (shutdown hook)."""
        for phone_id in list(self._phone_processes.keys()):
            try:
                await self.stop_for_phone(phone_id)
            except Exception:
                log.exception("cleanup: stop_for_phone(%s) failed", phone_id)
