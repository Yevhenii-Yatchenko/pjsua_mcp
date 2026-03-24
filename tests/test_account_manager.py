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

    def test_reconfigure_updates_credentials(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        mgr.configure(domain="old.example.com", username="alice", password="old")
        mgr.configure(domain="new.example.com", username="bob", password="new")
        assert mgr._domain == "new.example.com"
        assert mgr._username == "bob"
        assert mgr._password == "new"


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


class TestMessageQueue:
    def test_get_messages_no_account(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        assert mgr.get_messages() == []

    def test_get_messages_no_account_last_n(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        assert mgr.get_messages(last_n=5) == []

    def test_send_message_no_account_raises(self):
        engine = SipEngine()
        mgr = AccountManager(engine)
        with pytest.raises(RuntimeError, match="register first"):
            mgr.send_message("sip:bob@example.com", "hello")

    def test_sip_account_message_queue_empty_on_init(self):
        from src.account_manager import SipAccount
        acc = SipAccount()
        assert acc.get_messages() == []

    def test_sip_account_message_queue_last_n(self):
        from src.account_manager import SipAccount
        import pjsua2 as pj
        acc = SipAccount()
        # Manually inject messages to test last_n filtering
        for i in range(5):
            from datetime import datetime
            acc._messages.append({
                "from": f"sip:user{i}@example.com",
                "to": "sip:me@example.com",
                "body": f"message {i}",
                "content_type": "text/plain",
                "timestamp": datetime.now().isoformat(),
            })
        all_msgs = acc.get_messages()
        assert len(all_msgs) == 5
        last_2 = acc.get_messages(last_n=2)
        assert len(last_2) == 2
        assert last_2[0]["body"] == "message 3"
        assert last_2[1]["body"] == "message 4"
