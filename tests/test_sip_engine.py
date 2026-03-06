"""Unit tests for SipEngine — guards, validation, lifecycle."""

from __future__ import annotations

import pytest

from src.sip_engine import SipEngine


class TestInitializeValidation:
    def test_invalid_transport(self, pjsua_endpoint):
        engine = SipEngine()
        with pytest.raises(ValueError, match="Unsupported transport"):
            engine.initialize(transport="websocket")

    def test_double_init(self, pjsua_endpoint):
        """Cannot initialize twice on the same engine instance.

        We test this by initializing once on a *fresh* Endpoint. Since the
        session-scoped pjsua_endpoint fixture already owns the singleton,
        we test the guard by manually setting _initialized=True.
        """
        engine = SipEngine()
        engine._initialized = True
        with pytest.raises(RuntimeError, match="already initialized"):
            engine.initialize()


class TestShutdown:
    def test_idempotent(self):
        engine = SipEngine()
        # shutdown on never-initialized engine should be safe
        engine.shutdown()
        engine.shutdown()  # second call should also be fine


class TestGetLogEntries:
    def test_before_init(self):
        engine = SipEngine()
        assert engine.get_log_entries() == []


class TestInitializedProperty:
    def test_default_false(self):
        engine = SipEngine()
        assert engine.initialized is False
