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


class TestSdpCreatedCallback:
    """SipCall.onCallSdpCreated rewrites prm.sdp.wholeSdp via the
    rewriter when self._codecs is non-empty. Constructs SipCall via
    __new__ so we don't need a live pj.Account."""

    def _make_param(self, sdp_in: str, rem_sdp: str = ""):
        class _S:
            wholeSdp = ""
        prm = type("P", (), {})()
        prm.sdp = _S()
        prm.sdp.wholeSdp = sdp_in
        prm.remSdp = _S()
        prm.remSdp.wholeSdp = rem_sdp
        return prm

    def test_no_codecs_no_op(self):
        from src.call_manager import SipCall
        call = SipCall.__new__(SipCall)
        call._codecs = None
        call.phone_id = "a"
        prm = self._make_param("v=0\r\nm=audio 4000 RTP/AVP 0\r\n")
        call.onCallSdpCreated(prm)
        assert prm.sdp.wholeSdp == "v=0\r\nm=audio 4000 RTP/AVP 0\r\n"

    def test_filters_outgoing_offer(self):
        from src.call_manager import SipCall
        call = SipCall.__new__(SipCall)
        call._codecs = ["PCMA", "telephone-event"]
        call.phone_id = "a"
        sdp_in = (
            "v=0\r\nm=audio 4000 RTP/AVP 0 8 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
        )
        prm = self._make_param(sdp_in)
        call.onCallSdpCreated(prm)
        assert "m=audio 4000 RTP/AVP 8 101\r\n" in prm.sdp.wholeSdp
        assert "PCMU" not in prm.sdp.wholeSdp

    def test_filters_outgoing_answer(self):
        """remSdp populated → we are the answerer; same filter applies."""
        from src.call_manager import SipCall
        call = SipCall.__new__(SipCall)
        call._codecs = ["PCMA"]
        call.phone_id = "a"
        sdp_in = (
            "v=0\r\nm=audio 4000 RTP/AVP 0 8\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
        )
        rem_sdp = (
            "v=0\r\nm=audio 4002 RTP/AVP 0 8\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
        )
        prm = self._make_param(sdp_in, rem_sdp=rem_sdp)
        call.onCallSdpCreated(prm)
        assert "m=audio 4000 RTP/AVP 8\r\n" in prm.sdp.wholeSdp
        assert "PCMU" not in prm.sdp.wholeSdp

    def test_dtmf_preserved_implicitly(self):
        """preserve_dtmf=True is passed by SipCall — telephone-event
        survives even when the codec list is just media codecs."""
        from src.call_manager import SipCall
        call = SipCall.__new__(SipCall)
        call._codecs = ["PCMA"]
        call.phone_id = "a"
        sdp_in = (
            "v=0\r\nm=audio 4000 RTP/AVP 0 8 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
        )
        prm = self._make_param(sdp_in)
        call.onCallSdpCreated(prm)
        assert "telephone-event" in prm.sdp.wholeSdp
        assert "PCMA" in prm.sdp.wholeSdp
        assert "PCMU" not in prm.sdp.wholeSdp
