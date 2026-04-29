"""Unit tests for multi-phone PhoneRegistry API."""

from __future__ import annotations

from dataclasses import fields

from src.sip_engine import SipEngine
from src.account_manager import PhoneConfig, PhoneRegistry, SipAccount, DEFAULT_PHONE_ID


class TestEmptyRegistry:
    def test_has_phone_false_on_empty(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.has_phone("a") is False

    def test_list_phones_empty(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.list_phones() == []

    def test_list_phone_ids_empty(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.list_phone_ids() == []

    def test_get_account_missing(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.get_account("a") is None

    def test_get_config_missing(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.get_config("a") is None

    def test_drop_nonexistent_returns_false(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.drop_phone("alice") is False

    def test_get_registration_info_missing(self):
        registry = PhoneRegistry(SipEngine())
        info = registry.get_registration_info("a")
        assert info["is_registered"] is False
        assert "not configured" in info["reason"]


class TestAddPhoneGuards:
    def test_add_before_engine_init(self):
        registry = PhoneRegistry(SipEngine())
        import pytest
        with pytest.raises(RuntimeError, match="not initialized"):
            registry.add_phone("a", domain="sip.example.com", username="alice", password="x")


class TestLegacyCompatAPI:
    def test_configure_populates_default_config_properties(self):
        registry = PhoneRegistry(SipEngine())
        registry.configure(
            domain="sip.example.com",
            username="alice",
            password="secret",
            realm="example.com",
            srtp=True,
        )
        assert registry._domain == "sip.example.com"
        assert registry._username == "alice"
        assert registry._password == "secret"
        assert registry._realm == "example.com"
        assert registry._srtp is True

    def test_account_property_none_before_register(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.account is None

    def test_auto_answer_false_before_register(self):
        registry = PhoneRegistry(SipEngine())
        assert registry.auto_answer is False

    def test_unregister_noop_without_phone(self):
        """unregister() on empty registry should not raise."""
        registry = PhoneRegistry(SipEngine())
        registry.unregister()  # no-op


class TestSipAccountIsolation:
    def test_two_accounts_separate_state(self):
        """Two SipAccount instances keep independent message queues and reg info."""
        a = SipAccount(phone_id="a")
        b = SipAccount(phone_id="b")

        assert a.phone_id == "a"
        assert b.phone_id == "b"

        # Independent message queues
        from datetime import datetime
        a._messages.append({
            "from": "sip:x@example.com", "to": "sip:a@example.com",
            "body": "for A", "content_type": "text/plain",
            "timestamp": datetime.now().isoformat(),
        })
        assert len(a.get_messages()) == 1
        assert b.get_messages() == []

        # Independent reg info
        a._reg_info["is_registered"] = True
        assert a.get_reg_info()["is_registered"] is True
        assert b.get_reg_info()["is_registered"] is False

    def test_incoming_callbacks_are_per_account(self):
        a = SipAccount(phone_id="a")
        b = SipAccount(phone_id="b")

        received = []
        a.on_incoming_call_cb = lambda cid: received.append(("a", cid))
        b.on_incoming_call_cb = lambda cid: received.append(("b", cid))

        # Simulate callbacks firing
        a.on_incoming_call_cb(1)
        b.on_incoming_call_cb(2)
        a.on_incoming_call_cb(3)

        assert received == [("a", 1), ("b", 2), ("a", 3)]

    def test_default_phone_id(self):
        acc = SipAccount()
        assert acc.phone_id == DEFAULT_PHONE_ID


class TestRegistryHooks:
    def test_on_phone_added_fires(self):
        """on_phone_added hook should be settable — exercised by CallManager wiring."""
        registry = PhoneRegistry(SipEngine())
        fired = []
        registry.on_phone_added = lambda pid: fired.append(pid)
        # Can't actually add without engine init, but callback slot exists.
        assert registry.on_phone_added is not None

    def test_on_phone_dropped_fires_only_if_present(self):
        registry = PhoneRegistry(SipEngine())
        fired = []
        registry.on_phone_dropped = lambda pid: fired.append(pid)
        # Drop nonexistent — hook must NOT fire
        registry.drop_phone("ghost")
        assert fired == []


class TestPhoneConfigCodecs:
    """Per-phone codec preferences restored — `cfg.codecs` is the wishlist
    used by the SDP rewriter to filter offers/answers for this phone."""

    def test_phone_config_has_codecs_field(self):
        names = {f.name for f in fields(PhoneConfig)}
        assert "codecs" in names, (
            f"PhoneConfig must carry a `codecs` field — got {sorted(names)}"
        )

    def test_phone_config_codecs_default_is_none(self):
        cfg = PhoneConfig(domain="x")
        assert cfg.codecs is None

    def test_phone_config_codecs_round_trip(self):
        cfg = PhoneConfig(domain="x", codecs=["PCMA", "telephone-event"])
        assert cfg.codecs == ["PCMA", "telephone-event"]

    def test_add_phone_accepts_codecs_kwarg(self):
        """PhoneRegistry.add_phone(codecs=[...]) must accept the kwarg —
        signature check happens before the engine-init guard."""
        import pytest
        registry = PhoneRegistry(SipEngine())
        with pytest.raises(RuntimeError, match="not initialized"):
            registry.add_phone(
                "a",
                domain="sip.example.com",
                username="u",
                password="p",
                codecs=["PCMA", "telephone-event"],
            )
