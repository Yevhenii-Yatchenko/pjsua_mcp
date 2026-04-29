"""SDP audio-codec filter — line-by-line state machine, pure Python.

Reference impl: /home/wintery/repos/sippy/SdpBodySection.py:528
"""

from __future__ import annotations

from src.sdp_rewriter import filter_audio_codecs


SAMPLE_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 10.0.0.1\r\n"
    "s=pjmedia\r\n"
    "c=IN IP4 10.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 4000 RTP/AVP 0 8 9 18 101\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:9 G722/8000\r\n"
    "a=rtpmap:18 G729/8000\r\n"
    "a=rtpmap:101 telephone-event/8000\r\n"
    "a=fmtp:101 0-16\r\n"
    "a=ptime:20\r\n"
    "a=sendrecv\r\n"
)


class TestHappyPath:
    def test_keeps_only_listed_codecs(self):
        out = filter_audio_codecs(SAMPLE_SDP, ["PCMA", "telephone-event"])
        assert "m=audio 4000 RTP/AVP 8 101\r\n" in out
        assert "a=rtpmap:8 PCMA/8000\r\n" in out
        assert "a=rtpmap:101 telephone-event/8000\r\n" in out
        assert "a=fmtp:101 0-16\r\n" in out
        assert "PCMU" not in out
        assert "G722" not in out
        assert "G729" not in out
        assert "a=rtpmap:0" not in out
        assert "a=rtpmap:9" not in out
        assert "a=rtpmap:18" not in out
        assert out.startswith("v=0\r\n")
        assert "c=IN IP4 10.0.0.1\r\n" in out
        assert "t=0 0\r\n" in out
        assert "a=ptime:20\r\n" in out
        assert "a=sendrecv\r\n" in out


class TestEmptyAllowList:
    def test_empty_list_returns_sdp_unchanged(self):
        assert filter_audio_codecs(SAMPLE_SDP, []) == SAMPLE_SDP

    def test_none_list_treated_as_empty(self):
        assert filter_audio_codecs(SAMPLE_SDP, [None, "", None]) == SAMPLE_SDP

    def test_empty_sdp_returns_empty(self):
        assert filter_audio_codecs("", ["PCMA"]) == ""


class TestDtmfPreservation:
    def test_explicit_telephone_event_kept(self):
        out = filter_audio_codecs(SAMPLE_SDP, ["PCMA", "telephone-event"])
        assert "telephone-event" in out
        assert "a=rtpmap:101" in out

    def test_preserve_dtmf_flag_keeps_dtmf(self):
        out = filter_audio_codecs(SAMPLE_SDP, ["PCMA"], preserve_dtmf=True)
        assert "telephone-event" in out
        assert "a=rtpmap:101" in out
        assert "PCMA" in out
        assert "PCMU" not in out

    def test_no_preserve_drops_dtmf(self):
        out = filter_audio_codecs(SAMPLE_SDP, ["PCMA"])
        assert "PCMA" in out
        assert "telephone-event" not in out


MULTI_SECTION_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 10.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 10.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 4000 RTP/AVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=sendrecv\r\n"
    "m=video 4002 RTP/AVP 96\r\n"
    "a=rtpmap:96 H264/90000\r\n"
    "a=fmtp:96 profile-level-id=42e01e\r\n"
    "m=image 4004 udptl t38\r\n"
    "a=T38FaxMaxBuffer:200\r\n"
    "a=T38FaxMaxDatagram:180\r\n"
)


class TestMultiSection:
    def test_video_block_passthrough(self):
        out = filter_audio_codecs(MULTI_SECTION_SDP, ["PCMA"])
        assert "m=video 4002 RTP/AVP 96\r\n" in out
        assert "a=rtpmap:96 H264/90000\r\n" in out
        assert "a=fmtp:96 profile-level-id=42e01e\r\n" in out

    def test_image_t38_block_passthrough(self):
        out = filter_audio_codecs(MULTI_SECTION_SDP, ["PCMA"])
        assert "m=image 4004 udptl t38\r\n" in out
        assert "a=T38FaxMaxBuffer:200\r\n" in out
        assert "a=T38FaxMaxDatagram:180\r\n" in out

    def test_audio_block_filtered_in_multi_section(self):
        out = filter_audio_codecs(MULTI_SECTION_SDP, ["PCMA"])
        assert "m=audio 4000 RTP/AVP 8\r\n" in out
        assert "a=rtpmap:8 PCMA/8000\r\n" in out
        assert "PCMU" not in out
        assert "a=rtpmap:0" not in out


HOLD_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 10.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 10.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 4000 RTP/AVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=sendonly\r\n"
)


class TestHoldUnhold:
    def test_sendonly_preserved(self):
        out = filter_audio_codecs(HOLD_SDP, ["PCMA"])
        assert "a=sendonly\r\n" in out
        assert "a=sendrecv" not in out
        assert "a=rtpmap:8 PCMA/8000\r\n" in out
        assert "PCMU" not in out

    def test_inactive_preserved(self):
        sdp = HOLD_SDP.replace("a=sendonly", "a=inactive")
        out = filter_audio_codecs(sdp, ["PCMA"])
        assert "a=inactive\r\n" in out

    def test_recvonly_preserved(self):
        sdp = HOLD_SDP.replace("a=sendonly", "a=recvonly")
        out = filter_audio_codecs(sdp, ["PCMA"])
        assert "a=recvonly\r\n" in out


SRTP_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 10.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 10.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 4000 RTP/SAVP 0 8\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:abcd1234abcd1234abcd1234abcd1234abcd1234abcd\r\n"
    "a=crypto:2 AES_CM_128_HMAC_SHA1_32 inline:efgh5678efgh5678efgh5678efgh5678efgh5678efgh\r\n"
    "a=sendrecv\r\n"
)


class TestSrtpPassthrough:
    def test_crypto_lines_unchanged(self):
        out = filter_audio_codecs(SRTP_SDP, ["PCMA"])
        assert (
            "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:abcd1234abcd1234"
            "abcd1234abcd1234abcd1234abcd\r\n"
        ) in out
        assert (
            "a=crypto:2 AES_CM_128_HMAC_SHA1_32 inline:efgh5678efgh5678"
            "efgh5678efgh5678efgh5678efgh\r\n"
        ) in out

    def test_savp_proto_preserved(self):
        out = filter_audio_codecs(SRTP_SDP, ["PCMA"])
        assert "m=audio 4000 RTP/SAVP 8\r\n" in out


class TestEmptyResultGuard:
    def test_returns_unchanged_when_no_match(self):
        """If filtering would leave m=audio with zero formats — invalid
        SDP — return the input unchanged (pjsua falls back to global
        priorities then, which is the safest behaviour)."""
        out = filter_audio_codecs(SAMPLE_SDP, ["EVRC", "iLBC"])
        assert out == SAMPLE_SDP


OPUS_SDP = (
    "v=0\r\n"
    "o=- 0 0 IN IP4 10.0.0.1\r\n"
    "s=-\r\n"
    "c=IN IP4 10.0.0.1\r\n"
    "t=0 0\r\n"
    "m=audio 4000 RTP/AVP 8 18 111\r\n"
    "a=rtpmap:8 PCMA/8000\r\n"
    "a=rtpmap:18 G729/8000\r\n"
    "a=fmtp:18 annexb=no\r\n"
    "a=rtpmap:111 opus/48000/2\r\n"
    "a=fmtp:111 useinbandfec=1; minptime=10\r\n"
    "a=sendrecv\r\n"
)


class TestDynamicCodecsFmtp:
    def test_opus_kept_with_fmtp(self):
        out = filter_audio_codecs(OPUS_SDP, ["opus"])
        assert "m=audio 4000 RTP/AVP 111\r\n" in out
        assert "a=rtpmap:111 opus/48000/2\r\n" in out
        assert "a=fmtp:111 useinbandfec=1; minptime=10\r\n" in out
        assert "annexb=no" not in out
        assert "G729" not in out
        assert "a=rtpmap:18" not in out

    def test_g729_kept_with_fmtp(self):
        out = filter_audio_codecs(OPUS_SDP, ["G729"])
        assert "m=audio 4000 RTP/AVP 18\r\n" in out
        assert "a=rtpmap:18 G729/8000\r\n" in out
        assert "a=fmtp:18 annexb=no\r\n" in out
        assert "opus" not in out
        assert "useinbandfec" not in out


class TestParsingTolerance:
    def test_case_insensitive_match(self):
        out = filter_audio_codecs(SAMPLE_SDP, ["pcma", "Telephone-Event"])
        assert "PCMA" in out
        assert "telephone-event" in out

    def test_lf_only_input(self):
        sdp_lf = SAMPLE_SDP.replace("\r\n", "\n")
        out = filter_audio_codecs(sdp_lf, ["PCMA"])
        assert "\r" not in out
        assert "m=audio 4000 RTP/AVP 8\n" in out

    def test_static_pt_without_rtpmap(self):
        """Some Asterisk peers send PCMU/PCMA without rtpmap (static PTs)."""
        compact = (
            "v=0\r\nm=audio 4000 RTP/AVP 0 8\r\na=sendrecv\r\n"
        )
        out = filter_audio_codecs(compact, ["PCMA"])
        assert "m=audio 4000 RTP/AVP 8\r\n" in out
