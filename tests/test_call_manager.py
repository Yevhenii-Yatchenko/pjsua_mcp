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


class TestUnholdSetsCallSettingFlag:
    """Regression: `unhold()` must set `prm.opt.flag = PJSUA_CALL_UNHOLD`,
    NOT `prm.flag` (a Python-side attribute that pjsua's C++ side ignores).

    Without `prm.opt.flag` set, pjsua's `pjsua_call_reinvite` reuses the
    cached hold-state SDP — re-INVITE goes out with the same `o=` version
    and `a=sendonly`, the server (correctly) ignores it as "no change",
    and media stays held. See bug-pjsua-mcp-unhold-flag.md.
    """

    def _fake_call(self):
        """Stand-in for pj.Call that captures the prm passed to reinvite()."""
        captured: dict = {"prm": None, "called": False}

        class _FakeCall:
            def __init__(self) -> None:
                self.captured = captured

            def reinvite(self, prm) -> None:
                self.captured["prm"] = prm
                self.captured["called"] = True

        return _FakeCall(), captured

    def test_unhold_sets_opt_flag_to_unhold(self, call_mgr, monkeypatch):
        import pjsua2 as pj
        fake_call, captured = self._fake_call()
        # Plug the fake into the manager's call dict + phone mapping so
        # _ensure_call_belongs_to and _get_call_by_id both succeed.
        call_mgr._calls[42] = fake_call
        call_mgr._call_phone[42] = "a"
        monkeypatch.setattr(call_mgr, "_resolve_phone", lambda pid: "a")
        monkeypatch.setattr(call_mgr, "_ensure_call_belongs_to", lambda pid, cid: None)

        call_mgr.unhold(call_id=42, phone_id="a")

        assert captured["called"], "reinvite was not invoked"
        prm = captured["prm"]
        # The bug was setting prm.flag (a stray Python attribute) instead
        # of prm.opt.flag (the actual C++ struct field). Assert the
        # correct path holds the UNHOLD bit.
        assert prm.opt.flag == pj.PJSUA_CALL_UNHOLD, (
            f"Expected prm.opt.flag={pj.PJSUA_CALL_UNHOLD} "
            f"(PJSUA_CALL_UNHOLD), got {prm.opt.flag}. "
            "The unhold re-INVITE will reuse the held SDP and stay "
            "one-way at the SDP layer."
        )
        # Without `opt.audioCount = 1`, pjsua's reinvite emits an
        # `m=audio 0` "rejected media" SDP and the call has no audio
        # stream advertised after unhold — even though the UNHOLD bit
        # was honoured.
        assert prm.opt.audioCount == 1, (
            f"Expected prm.opt.audioCount=1, got {prm.opt.audioCount}. "
            "Unhold re-INVITE will mark audio as disabled."
        )
