"""PJSUA2 Endpoint lifecycle, transport, and event loop management."""

from __future__ import annotations

import logging
import threading

import pjsua2 as pj

from .sip_logger import SipLogWriter

log = logging.getLogger(__name__)

TRANSPORT_MAP = {
    "udp": pj.PJSIP_TRANSPORT_UDP,
    "tcp": pj.PJSIP_TRANSPORT_TCP,
    "tls": pj.PJSIP_TRANSPORT_TLS,
}


class SipEngine:
    """Manages the PJSUA2 Endpoint singleton."""

    def __init__(self) -> None:
        self._ep: pj.Endpoint | None = None
        self._log_writer: SipLogWriter | None = None
        self._initialized = False
        self._lock = threading.Lock()

    @property
    def ep(self) -> pj.Endpoint:
        assert self._ep is not None, "Endpoint not initialized — call initialize() first"
        return self._ep

    @property
    def initialized(self) -> bool:
        return self._initialized

    def initialize(self) -> None:
        """Create Endpoint and start the pjlib library.

        Transports are created separately via `create_transport()` — one per phone.
        """
        if self._initialized:
            raise RuntimeError("SIP engine already initialized")

        ep = pj.Endpoint()
        ep.libCreate()

        ep_cfg = pj.EpConfig()

        # Logging: capture everything via LogWriter.
        # consoleLevel must match level — pjsua_start() calls pj_log_set_level(consoleLevel)
        # which sets the GLOBAL log level, suppressing the writer too if set to 0.
        # C-level stdout is redirected to stderr in server.py so console output is safe.
        self._log_writer = SipLogWriter()
        ep_cfg.logConfig.level = 5
        ep_cfg.logConfig.consoleLevel = 5
        ep_cfg.logConfig.writer = self._log_writer

        # Threading: 0 internal threads — we poll manually for Python safety
        ep_cfg.uaConfig.threadCnt = 0
        ep_cfg.uaConfig.mainThreadOnly = True

        ep.libInit(ep_cfg)

        # Null audio device — headless Docker, no sound card
        ep.audDevManager().setNullDev()

        ep.libStart()

        self._ep = ep
        self._initialized = True
        log.info("SIP engine initialized (no transports — created per-phone)")

    def create_transport(self, transport: str = "udp", local_port: int = 0) -> int:
        """Create a new transport bound to `local_port` and return its ID.

        Each phone (pj.Account) gets its own transport so per-phone packet
        capture and SIP-Contact port separation work. `local_port=0` lets the
        kernel pick an ephemeral port.
        """
        transport = transport.lower()
        if transport not in TRANSPORT_MAP:
            raise ValueError(f"Unsupported transport: {transport!r} (use udp/tcp/tls)")

        if not self._initialized:
            raise RuntimeError("SIP engine not initialized — call initialize() first")

        tp_cfg = pj.TransportConfig()
        tp_cfg.port = local_port
        tp_id = self._ep.transportCreate(TRANSPORT_MAP[transport], tp_cfg)
        log.info("Created %s transport id=%d (port=%d)", transport, tp_id, local_port)
        return tp_id

    def close_transport(self, transport_id: int) -> None:
        """Close a transport. Safe to call if the ID is unknown — logs and skips."""
        if not self._initialized or self._ep is None:
            return
        try:
            self._ep.transportClose(transport_id)
            log.info("Closed transport id=%d", transport_id)
        except Exception:
            log.exception("Failed to close transport id=%d", transport_id)

    def get_transport_port(self, transport_id: int) -> int | None:
        """Return the local port of a transport, or None if unknown."""
        if not self._initialized or self._ep is None:
            return None
        try:
            info = self._ep.transportGetInfo(transport_id)
            return info.localName.split(":")[-1] and int(info.localName.split(":")[-1])
        except Exception:
            return None

    def handle_events(self, msec_timeout: int = 50) -> None:
        """Poll PJSUA2 event loop. Called from executor thread."""
        if self._ep and self._initialized:
            self._register_thread()
            self._ep.libHandleEvents(msec_timeout)

    def register_current_thread(self) -> None:
        """Register the calling thread with pjlib (for external threads)."""
        self._register_thread()

    def _register_thread(self) -> None:
        """Register current thread with pjlib if not already registered."""
        if self._ep and not self._ep.libIsThreadRegistered():
            self._ep.libRegisterThread(threading.current_thread().name)

    def set_codecs(self, codecs: list[str]) -> list[dict]:
        """Set codec priorities. Codecs in the list are enabled in order;
        all others are disabled (priority=0).

        Codec names can be short ("PCMU", "G722") or full ("PCMU/8000/1").
        Returns the resulting codec list with priorities.
        """
        if not self._ep:
            raise RuntimeError("SIP engine not initialized")

        # Get all available codecs
        all_codecs = self._ep.codecEnum2()
        all_ids = [c.codecId for c in all_codecs]

        # Resolve short names to full codec IDs
        def _resolve(name: str) -> str | None:
            name_upper = name.upper()
            for cid in all_ids:
                if cid.upper().startswith(name_upper):
                    return cid
            return None

        # Disable all codecs first
        for cid in all_ids:
            self._ep.codecSetPriority(cid, 0)

        # Enable requested codecs in priority order (first = highest)
        enabled = []
        for i, name in enumerate(codecs):
            cid = _resolve(name)
            if cid:
                priority = 255 - i  # first codec gets 255, second 254, etc.
                self._ep.codecSetPriority(cid, priority)
                enabled.append({"codec": cid, "priority": priority})
                log.info("Codec %s priority=%d", cid, priority)
            else:
                log.warning("Codec not found: %s", name)

        return enabled

    def get_codecs(self) -> list[dict]:
        """Return all codecs with their current priorities."""
        if not self._ep:
            return []
        return [
            {"codec": c.codecId, "priority": c.priority}
            for c in self._ep.codecEnum2()
        ]

    def get_log_entries(
        self,
        last_n: int | None = None,
        filter_text: str | None = None,
    ) -> list[dict]:
        if self._log_writer is None:
            return []
        return self._log_writer.get_entries(last_n=last_n, filter_text=filter_text)

    def shutdown(self) -> None:
        """Graceful teardown of the PJSUA2 library."""
        if self._ep and self._initialized:
            log.info("Destroying PJSUA2 library")
            try:
                self._ep.libDestroy()
            except Exception:
                log.exception("Error during libDestroy")
            self._ep = None
            self._initialized = False
