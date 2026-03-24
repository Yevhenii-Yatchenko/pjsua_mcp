"""SIP account management — registration, credentials, incoming call routing."""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

import pjsua2 as pj

from .sip_engine import SipEngine

log = logging.getLogger(__name__)


class SipAccount(pj.Account):
    """PJSUA2 Account subclass with registration and incoming-call callbacks."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._reg_info: dict[str, Any] = {
            "is_registered": False,
            "status_code": 0,
            "reason": "",
            "expires": 0,
        }
        self._incoming_calls: deque[pj.CallInfo] = deque(maxlen=32)
        self.on_incoming_call_cb: Any = None  # set by CallManager
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
            "Registration state: active=%s status=%d %s",
            info.regIsActive, info.regStatus, info.regStatusText,
        )

    def onIncomingCall(self, prm: pj.OnIncomingCallParam) -> None:
        log.info("Incoming call: call_id=%d", prm.callId)
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
        log.info("Received MESSAGE from %s: %s", prm.fromUri, prm.msgBody[:50])

    def get_messages(self, last_n: int | None = None) -> list[dict]:
        with self._lock:
            msgs = list(self._messages)
        if last_n:
            msgs = msgs[-last_n:]
        return msgs

    def get_reg_info(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._reg_info)


class AccountManager:
    """High-level account operations — configure, register, unregister."""

    def __init__(self, engine: SipEngine) -> None:
        self._engine = engine
        self._account: SipAccount | None = None
        self._domain: str | None = None
        self._username: str | None = None
        self._password: str | None = None
        self._realm: str | None = None
        self._srtp: bool = False
        self._auto_answer: bool = False

    @property
    def account(self) -> SipAccount | None:
        return self._account

    @property
    def auto_answer(self) -> bool:
        return self._auto_answer

    def configure(
        self,
        domain: str,
        username: str | None = None,
        password: str | None = None,
        realm: str | None = None,
        srtp: bool = False,
        auto_answer: bool = False,
    ) -> None:
        """Store credentials for later registration."""
        self._domain = domain
        self._username = username
        self._password = password
        self._realm = realm or "*"
        self._srtp = srtp
        self._auto_answer = auto_answer

    def register(self) -> None:
        """Create account and send REGISTER."""
        if not self._engine.initialized:
            raise RuntimeError("SIP engine not initialized — call configure tool first")
        if self._domain is None:
            raise RuntimeError("Account not configured — call configure tool first")

        acc_cfg = pj.AccountConfig()

        # Build SIP ID and registrar URI
        if self._username:
            acc_cfg.idUri = f"sip:{self._username}@{self._domain}"
        else:
            acc_cfg.idUri = f"sip:{self._domain}"

        acc_cfg.regConfig.registrarUri = f"sip:{self._domain}"
        acc_cfg.regConfig.retryIntervalSec = 30

        # Credentials
        if self._username and self._password:
            cred = pj.AuthCredInfo()
            cred.scheme = "digest"
            cred.realm = self._realm or "*"
            cred.username = self._username
            cred.dataType = 0  # plain text password
            cred.data = self._password
            acc_cfg.sipConfig.authCreds.append(cred)

        # SRTP
        if self._srtp:
            acc_cfg.mediaConfig.srtpUse = pj.PJMEDIA_SRTP_MANDATORY
            acc_cfg.mediaConfig.srtpSecureSignaling = 0

        account = SipAccount()
        account.create(acc_cfg)
        self._account = account
        log.info("Account created and REGISTER sent for %s", acc_cfg.idUri)

    def unregister(self) -> None:
        """Send unREGISTER for current account."""
        if self._account and self._account.isValid():
            self._account.setRegistration(False)
            log.info("Unregister sent")

    def unregister_all(self) -> None:
        """Unregister and delete account (cleanup)."""
        if self._account:
            try:
                if self._account.isValid():
                    self._account.setRegistration(False)
                    self._account.shutdown()
            except Exception:
                log.exception("Error during account cleanup")
            self._account = None

    def get_registration_info(self) -> dict[str, Any]:
        if self._account is None:
            return {
                "is_registered": False,
                "status_code": 0,
                "reason": "No account configured",
                "expires": 0,
            }
        return self._account.get_reg_info()

    def send_message(self, dest_uri: str, body: str, content_type: str = "text/plain") -> None:
        """Send SIP MESSAGE via a temporary Buddy object."""
        if not self._account or not self._account.isValid():
            raise RuntimeError("No valid account — register first")
        buddy_cfg = pj.BuddyConfig()
        buddy_cfg.uri = dest_uri
        buddy = pj.Buddy()
        buddy.create(self._account, buddy_cfg)
        prm = pj.SendInstantMessageParam()
        prm.content = body
        prm.contentType = content_type
        buddy.sendInstantMessage(prm)

    def get_messages(self, last_n: int | None = None) -> list[dict]:
        if self._account is None:
            return []
        return self._account.get_messages(last_n=last_n)
