"""Unit tests for CallManager — lookup logic and guards."""

from __future__ import annotations

import pytest

from src.sip_engine import SipEngine
from src.account_manager import AccountManager
from src.call_manager import CallManager


@pytest.fixture()
def call_mgr():
    engine = SipEngine()
    acc_mgr = AccountManager(engine)
    return CallManager(engine, acc_mgr)


class TestGetCallById:
    def test_not_found(self, call_mgr):
        with pytest.raises(RuntimeError, match="not found"):
            call_mgr._get_call_by_id(9999)


class TestGetCall:
    def test_no_active(self, call_mgr):
        with pytest.raises(RuntimeError, match="No active call"):
            call_mgr._get_call()


class TestHangupAll:
    def test_empty(self, call_mgr):
        # Should not raise with no calls
        call_mgr.hangup_all()


class TestCallHistory:
    def test_empty_history(self, call_mgr):
        assert call_mgr.get_call_history() == []
