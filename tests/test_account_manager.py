"""Unit tests for AccountManager — configure, guards, defaults."""

from __future__ import annotations

import pytest

from src.sip_engine import SipEngine
from src.account_manager import AccountManager


class TestConfigure:
    def test_stores_credentials(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        mgr.configure(
            domain="sip.example.com",
            username="alice",
            password="secret",
            realm="example.com",
            srtp=True,
        )
        assert mgr._domain == "sip.example.com"
        assert mgr._username == "alice"
        assert mgr._password == "secret"
        assert mgr._realm == "example.com"
        assert mgr._srtp is True

    def test_realm_defaults_to_star(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        mgr.configure(domain="sip.example.com")
        assert mgr._realm == "*"


class TestGetRegistrationInfo:
    def test_no_account(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        info = mgr.get_registration_info()
        assert info["is_registered"] is False
        assert info["status_code"] == 0


class TestRegisterGuards:
    def test_before_engine_init(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        mgr.configure(domain="sip.example.com", username="alice", password="pw")
        with pytest.raises(RuntimeError, match="not initialized"):
            mgr.register()

    def test_before_configure(self):
        engine = SipEngine()
        engine._initialized = True  # fake it
        mgr = AccountManager(engine)
        with pytest.raises(RuntimeError, match="not configured"):
            mgr.register()
