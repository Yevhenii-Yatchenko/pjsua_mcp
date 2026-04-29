"""SIP account management — multi-phone registry, credentials, incoming-call routing."""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import pjsua2 as pj

from .scenario_engine.event_bus import Event, emit_global
from .sip_engine import SipEngine

log = logging.getLogger(__name__)

DEFAULT_PHONE_ID = "default"


@dataclass
class PhoneConfig:
    """Per-phone configuration stored in the PhoneRegistry."""

    domain: str
    username: str | None = None
    password: str | None = None
    realm: str = "*"
    srtp: bool = False
    auto_answer: bool = False
    transport: str = "udp"
    local_port: int = 0
    codecs: list[str] | None = None
    transport_id: int | None = None
    recording_enabled: bool = False
    capture_enabled: bool = False


class SipAccount(pj.Account):
    """PJSUA2 Account subclass with registration and incoming-call callbacks."""

    def __init__(self, phone_id: str = DEFAULT_PHONE_ID) -> None:
        super().__init__()
        self.phone_id = phone_id
        self._lock = threading.Lock()
        self._reg_info: dict[str, Any] = {
            "is_registered": False,
            "status_code": 0,
            "reason": "",
            "expires": 0,
        }
        self._incoming_calls: deque[pj.CallInfo] = deque(maxlen=32)
        self.on_incoming_call_cb: Callable[[int], None] | None = None  # set by CallManager
        self._messages: deque[dict] = deque(maxlen=100)

    def onRegState(self, prm: pj.OnRegStateParam) -> None:
        info = self.getInfo()
        with self._lock:
            self._reg_info = {
                "is_registered": info.regIsActive,
                "status_code": info.regStatus,
                "reason": info.regStatusText,
                "expires": info.regExpiresSec,
            }
        log.info(
            "[%s] Registration state: active=%s status=%d %s",
            self.phone_id, info.regIsActive, info.regStatus, info.regStatusText,
        )
        if info.regIsActive:
            evt_type = "reg.success"
        elif info.regStatus == 0:
            evt_type = "reg.started"
        elif info.regExpiresSec == 0:
            evt_type = "reg.unregistered"
        else:
            evt_type = "reg.failed"
        emit_global(
            Event(
                type=evt_type,
                phone_id=self.phone_id,
                data={
                    "status_code": info.regStatus,
                    "reason": info.regStatusText,
                    "expires": info.regExpiresSec,
                },
            )
        )

    def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:
        log.info("[%s] Incoming call: call_id=%d", self.phone_id, prm.callId)
        if self.on_incoming_call_cb:
            self.on_incoming_call_cb(prm.callId)

    def onInstantMessage(self, prm: pj.OnInstantMessageParam) -> None:
        from datetime import datetime
        msg = {
            "from": prm.fromUri,
            "to": prm.toUri,
            "body": prm.msgBody,
            "content_type": prm.contentType,
            "timestamp": datetime.now().isoformat(),
        }
        with self._lock:
            self._messages.append(msg)
        log.info("[%s] Received MESSAGE from %s: %s", self.phone_id, prm.fromUri, prm.msgBody[:50])
        emit_global(
            Event(
                type="im.received",
                phone_id=self.phone_id,
                data={
                    "from": prm.fromUri,
                    "to": prm.toUri,
                    "body": prm.msgBody,
                    "content_type": prm.contentType,
                },
            )
        )

    def get_messages(self, last_n: int | None = None) -> list[dict]:
        with self._lock:
            msgs = list(self._messages)
        if last_n:
            msgs = msgs[-last_n:]
        return msgs

    def get_reg_info(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._reg_info)


class PhoneRegistry:
    """Multi-phone registry — holds N SipAccount objects keyed by phone_id.

    Each phone has its own transport, credentials, auto-answer flag, and
    per-phone codec preference list.
    """

    def __init__(self, engine: SipEngine) -> None:
        self._engine = engine
        self._accounts: dict[str, SipAccount] = {}
        self._configs: dict[str, PhoneConfig] = {}
        self._legacy_pending: PhoneConfig | None = None
        self._lock = threading.Lock()
        # Callback hook — CallManager subscribes to get (phone_id, call_id) tuples
        # whenever a new phone is added, so it can wire up incoming-call routing.
        self.on_phone_added: Callable[[str], None] | None = None
        self.on_phone_dropped: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # Core multi-phone API
    # ------------------------------------------------------------------
    def add_phone(
        self,
        phone_id: str,
        domain: str,
        username: str | None = None,
        password: str | None = None,
        *,
        realm: str | None = None,
        srtp: bool = False,
        auto_answer: bool = False,
        transport: str = "udp",
        local_port: int = 0,
        register: bool = True,
        recording_enabled: bool = False,
        capture_enabled: bool = False,
    ) -> SipAccount:
        """Create a transport, a SipAccount, and (optionally) REGISTER.

        If `phone_id` already exists, the old account is torn down first
        (transport closed, account shut down) before creating the new one.
        """
        if not self._engine.initialized:
            raise RuntimeError("SIP engine not initialized — call engine.initialize() first")

        # Replace if already exists
        if phone_id in self._accounts:
            log.info("[%s] Replacing existing phone", phone_id)
            self.drop_phone(phone_id)

        cfg = PhoneConfig(
            domain=domain,
            username=username,
            password=password,
            realm=realm or "*",
            srtp=srtp,
            auto_answer=auto_answer,
            transport=transport,
            local_port=local_port,
            recording_enabled=recording_enabled,
            capture_enabled=capture_enabled,
        )

        # Per-phone transport
        tp_id = self._engine.create_transport(transport=transport, local_port=local_port)
        cfg.transport_id = tp_id

        # Build AccountConfig
        acc_cfg = pj.AccountConfig()
        if cfg.username:
            acc_cfg.idUri = f"sip:{cfg.username}@{cfg.domain}"
        else:
            acc_cfg.idUri = f"sip:{cfg.domain}"
        acc_cfg.regConfig.registrarUri = f"sip:{cfg.domain}"
        acc_cfg.regConfig.retryIntervalSec = 30
        acc_cfg.regConfig.registerOnAdd = register
        acc_cfg.sipConfig.transportId = tp_id

        if cfg.username and cfg.password:
            cred = pj.AuthCredInfo()
            cred.scheme = "digest"
            cred.realm = cfg.realm
            cred.username = cfg.username
            cred.dataType = 0
            cred.data = cfg.password
            acc_cfg.sipConfig.authCreds.append(cred)

        if cfg.srtp:
            acc_cfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY
            acc_cfg.mediaConfig.srtpSecureSignaling = 0

        account = SipAccount(phone_id=phone_id)
        account.create(acc_cfg)

        with self._lock:
            self._accounts[phone_id] = account
            self._configs[phone_id] = cfg

        if self.on_phone_added:
            try:
                self.on_phone_added(phone_id)
            except Exception:
                log.exception("on_phone_added hook failed for %s", phone_id)

        log.info(
            "[%s] Phone added (idUri=%s, transport_id=%d, auto_answer=%s)",
            phone_id, acc_cfg.idUri, tp_id, auto_answer,
        )
        return account

    def drop_phone(self, phone_id: str) -> bool:
        """Tear down a phone: unregister, shutdown account, close transport.

        Returns True if the phone was present, False otherwise.
        """
        with self._lock:
            account = self._accounts.pop(phone_id, None)
            cfg = self._configs.pop(phone_id, None)

        if account is None:
            return False

        if self.on_phone_dropped:
            try:
                self.on_phone_dropped(phone_id)
            except Exception:
                log.exception("on_phone_dropped hook failed for %s", phone_id)

        try:
            if account.isValid():
                try:
                    account.setRegistration(False)
                except Exception:
                    log.debug("[%s] setRegistration(False) failed", phone_id)
                try:
                    account.shutdown()
                except Exception:
                    log.debug("[%s] account.shutdown() failed", phone_id)
        except Exception:
            log.exception("[%s] Error during account teardown", phone_id)

        if cfg and cfg.transport_id is not None:
            self._engine.close_transport(cfg.transport_id)

        log.info("[%s] Phone dropped", phone_id)
        return True

    def get_account(self, phone_id: str) -> SipAccount | None:
        with self._lock:
            return self._accounts.get(phone_id)

    def get_config(self, phone_id: str) -> PhoneConfig | None:
        with self._lock:
            return self._configs.get(phone_id)

    def require_account(self, phone_id: str) -> SipAccount:
        acc = self.get_account(phone_id)
        if acc is None:
            raise RuntimeError(f"Phone {phone_id!r} not found — add_phone first")
        if not acc.isValid():
            raise RuntimeError(f"Phone {phone_id!r} account is invalid")
        return acc

    def has_phone(self, phone_id: str) -> bool:
        with self._lock:
            return phone_id in self._accounts

    def list_phone_ids(self) -> list[str]:
        with self._lock:
            return list(self._accounts.keys())

    def list_phones(self) -> list[dict[str, Any]]:
        """Return summary of each phone — reg state, credentials (sans password)."""
        result = []
        with self._lock:
            items = list(self._accounts.items())
        for pid, acc in items:
            cfg = self._configs.get(pid)
            reg = acc.get_reg_info()
            entry = {
                "phone_id": pid,
                "domain": cfg.domain if cfg else "",
                "username": cfg.username if cfg else None,
                "auto_answer": cfg.auto_answer if cfg else False,
                "transport": cfg.transport if cfg else "udp",
                "local_port": cfg.local_port if cfg else 0,
                "transport_id": cfg.transport_id if cfg else None,
                "recording_enabled": cfg.recording_enabled if cfg else False,
                "capture_enabled": cfg.capture_enabled if cfg else False,
                **reg,
            }
            result.append(entry)
        return result

    def unregister_phone(self, phone_id: str) -> None:
        """Send de-REGISTER for a phone but keep the account alive."""
        acc = self.require_account(phone_id)
        acc.setRegistration(False)
        log.info("[%s] Unregister sent", phone_id)

    def reregister_phone(self, phone_id: str, force_unregister_first: bool = True) -> SipAccount:
        """Force a fresh REGISTER cycle by tearing down and recreating the account."""
        cfg = self.get_config(phone_id)
        if cfg is None:
            raise RuntimeError(f"Phone {phone_id!r} not found")
        # Replace re-uses add_phone path which shuts down the old account first.
        return self.add_phone(
            phone_id,
            domain=cfg.domain,
            username=cfg.username,
            password=cfg.password,
            realm=cfg.realm,
            srtp=cfg.srtp,
            auto_answer=cfg.auto_answer,
            transport=cfg.transport,
            local_port=cfg.local_port,
            register=True,
            recording_enabled=cfg.recording_enabled,
            capture_enabled=cfg.capture_enabled,
        )

    def send_message(
        self,
        dest_uri: str,
        body: str,
        phone_id: str = DEFAULT_PHONE_ID,
        content_type: str = "text/plain",
    ) -> None:
        acc = self.get_account(phone_id)
        if acc is None or not acc.isValid():
            raise RuntimeError(f"Phone {phone_id!r} has no valid account — register first")
        buddy_cfg = pj.BuddyConfig()
        buddy_cfg.uri = dest_uri
        buddy = pj.Buddy()
        buddy.create(acc, buddy_cfg)
        prm = pj.SendInstantMessageParam()
        prm.content = body
        prm.contentType = content_type
        buddy.sendInstantMessage(prm)

    def get_messages(
        self,
        phone_id: str = DEFAULT_PHONE_ID,
        last_n: int | None = None,
    ) -> list[dict]:
        acc = self.get_account(phone_id)
        if acc is None:
            return []
        return acc.get_messages(last_n=last_n)

    def get_registration_info(self, phone_id: str = DEFAULT_PHONE_ID) -> dict[str, Any]:
        acc = self.get_account(phone_id)
        if acc is None:
            return {
                "is_registered": False,
                "status_code": 0,
                "reason": "Phone not configured",
                "expires": 0,
            }
        return acc.get_reg_info()

    def drop_all(self) -> None:
        """Tear down every phone (called on server shutdown)."""
        for pid in self.list_phone_ids():
            self.drop_phone(pid)

    # ------------------------------------------------------------------
    # Legacy single-account API (delegates to phone_id="default")
    # Kept so existing `configure` / `register` / `unregister` tools
    # continue to function unchanged during Phase 1.
    # ------------------------------------------------------------------
    @property
    def account(self) -> SipAccount | None:
        return self.get_account(DEFAULT_PHONE_ID)

    @property
    def auto_answer(self) -> bool:
        cfg = self.get_config(DEFAULT_PHONE_ID)
        return bool(cfg and cfg.auto_answer)

    # Legacy attribute-style access used by tests/test_account_manager.py.
    # `_legacy_pending` holds the kwargs stashed by a legacy `configure()` call
    # before `register()`; after registration we read from the per-phone config.
    def _legacy_cfg(self) -> PhoneConfig | None:
        return self._legacy_pending or self.get_config(DEFAULT_PHONE_ID)

    @property
    def _domain(self) -> str | None:
        cfg = self._legacy_cfg()
        return cfg.domain if cfg else None

    @property
    def _username(self) -> str | None:
        cfg = self._legacy_cfg()
        return cfg.username if cfg else None

    @property
    def _password(self) -> str | None:
        cfg = self._legacy_cfg()
        return cfg.password if cfg else None

    @property
    def _realm(self) -> str | None:
        cfg = self._legacy_cfg()
        return cfg.realm if cfg else None

    @property
    def _srtp(self) -> bool:
        cfg = self._legacy_cfg()
        return bool(cfg and cfg.srtp)

    def configure(
        self,
        domain: str,
        username: str | None = None,
        password: str | None = None,
        realm: str | None = None,
        srtp: bool = False,
        auto_answer: bool = False,
        transport: str = "udp",
        local_port: int = 0,
    ) -> None:
        """Legacy: stash credentials for the "default" phone until register() is called."""
        self._legacy_pending = PhoneConfig(
            domain=domain,
            username=username,
            password=password,
            realm=realm or "*",
            srtp=srtp,
            auto_answer=auto_answer,
            transport=transport,
            local_port=local_port,
        )

    def register(self) -> None:
        """Legacy: create + register the "default" phone using stashed config."""
        cfg = self._legacy_pending
        if cfg is None:
            raise RuntimeError("Account not configured — call configure tool first")
        self.add_phone(
            DEFAULT_PHONE_ID,
            domain=cfg.domain,
            username=cfg.username,
            password=cfg.password,
            realm=cfg.realm,
            srtp=cfg.srtp,
            auto_answer=cfg.auto_answer,
            transport=cfg.transport,
            local_port=cfg.local_port,
            register=True,
        )

    def unregister(self) -> None:
        """Legacy: de-REGISTER the "default" phone."""
        if self.has_phone(DEFAULT_PHONE_ID):
            self.unregister_phone(DEFAULT_PHONE_ID)

    def unregister_all(self) -> None:
        """Legacy: drop the default phone (used on server shutdown)."""
        self.drop_all()


# Backward-compat alias — existing callers use AccountManager(engine).
AccountManager = PhoneRegistry
