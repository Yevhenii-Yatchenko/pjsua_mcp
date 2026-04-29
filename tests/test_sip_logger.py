"""Unit tests for SipLogWriter / LogEntry / parse_sip_metadata."""

from __future__ import annotations

from collections import deque

import pytest

from src.sip_logger import (
    LogEntry,
    PhoneMeta,
    SipLogWriter,
    filter_entries_by_owner,
    parse_sdp_body,
    parse_sip_headers,
    parse_sip_metadata,
    structurize_message,
)


def _make_writer(entries: list[LogEntry] | None = None, max_entries: int = 5000) -> SipLogWriter:
    """Create a SipLogWriter and optionally pre-populate its deque."""
    writer = SipLogWriter(max_entries=max_entries)
    if entries:
        writer._entries = deque(entries, maxlen=max_entries)
    return writer


def _entry(level: int = 3, msg: str = "test", thread: str = "main") -> LogEntry:
    return LogEntry(level=level, msg=msg, thread_name=thread)


class TestGetEntries:
    def test_returns_all(self):
        entries = [_entry(msg=f"msg{i}") for i in range(3)]
        w = _make_writer(entries)
        result = w.get_entries()
        assert len(result) == 3

    def test_last_n(self):
        entries = [_entry(msg=f"msg{i}") for i in range(5)]
        w = _make_writer(entries)
        result = w.get_entries(last_n=2)
        assert len(result) == 2
        assert result[0]["msg"] == "msg3"
        assert result[1]["msg"] == "msg4"

    def test_filter_text(self):
        entries = [
            _entry(msg="REGISTER sip:example.com"),
            _entry(msg="INVITE sip:bob@example.com"),
            _entry(msg="200 OK REGISTER"),
        ]
        w = _make_writer(entries)
        result = w.get_entries(filter_text="REGISTER")
        assert len(result) == 2

    def test_filter_and_last_n(self):
        entries = [
            _entry(msg="REGISTER 1"),
            _entry(msg="INVITE 1"),
            _entry(msg="REGISTER 2"),
            _entry(msg="REGISTER 3"),
        ]
        w = _make_writer(entries)
        result = w.get_entries(filter_text="REGISTER", last_n=1)
        assert len(result) == 1
        assert result[0]["msg"] == "REGISTER 3"


class TestBoundedDeque:
    def test_oldest_dropped(self):
        max_entries = 10
        entries = [_entry(msg=f"msg{i}") for i in range(max_entries + 1)]
        w = _make_writer(entries, max_entries=max_entries)
        result = w.get_entries()
        assert len(result) == max_entries
        # msg0 should have been dropped
        assert result[0]["msg"] == "msg1"


class TestClear:
    def test_clear_empties_deque(self):
        w = _make_writer([_entry(), _entry()])
        w.clear()
        assert w.get_entries() == []


class TestEntryFormat:
    def test_keys(self):
        w = _make_writer([_entry(level=4, msg="hello", thread="worker")])
        result = w.get_entries()
        assert len(result) == 1
        entry = result[0]
        assert entry["level"] == 4
        assert entry["msg"] == "hello"
        assert entry["thread"] == "worker"


# ---------------------------------------------------------------------------
# parse_sip_metadata — best-effort structured pull from a pjlib log entry.
# Each test mirrors a real pjsua-emitted message so the parser is grounded
# in actual log shapes rather than imagined ones.
# ---------------------------------------------------------------------------

TX_INVITE_MSG = """\
17:33:44.234   pjsua_core.c  TX 1128 bytes Request msg INVITE/cseq=8398 (tdta0xabc) to UDP 192.168.1.202:5060:
INVITE sip:123002@192.168.1.202 SIP/2.0
Via: SIP/2.0/UDP 192.168.1.40:36771;rport;branch=z9hG4bK-d-1
Max-Forwards: 70
From: "Alice" <sip:123001@192.168.1.202>;tag=alice-tag
To: <sip:123002@192.168.1.202>
Contact: <sip:123001@192.168.1.40:36771>
Call-ID: 5d0cbc47-e4b5-4e7a-b868-5fb3138f367d
CSeq: 8398 INVITE
User-Agent: PJSUA v2.14.1
Content-Type: application/sdp
Content-Length: 316

v=0
o=- 3986467525 3986467525 IN IP4 192.168.1.40
s=pjmedia
t=0 0
m=audio 4000 RTP/AVP 0 120
a=rtpmap:0 PCMU/8000
a=rtpmap:120 telephone-event/8000
"""

RX_200_OK_MSG = """\
17:33:45.555   pjsua_core.c  RX 956 bytes Response msg 200/INVITE/cseq=8398 (rdata0xdef) from UDP 192.168.1.202:5060:
SIP/2.0 200 OK
Via: SIP/2.0/UDP 192.168.1.40:36771;rport=36771;received=192.168.1.40;branch=z9hG4bK-d-1
From: "Alice" <sip:123001@192.168.1.202>;tag=alice-tag
To: <sip:123002@192.168.1.202>;tag=bob-tag
Call-ID: 5d0cbc47-e4b5-4e7a-b868-5fb3138f367d
CSeq: 8398 INVITE
Contact: <sip:123002@192.168.1.202:5060>
Content-Type: application/sdp
Content-Length: 200

v=0
m=audio 17000 RTP/AVP 0
a=rtpmap:0 PCMU/8000
"""

TX_REGISTER_MSG = """\
17:33:40.111   pjsua_acc.c   TX 612 bytes Request msg REGISTER/cseq=8397 (tdta0x111) to UDP 192.168.1.202:5060:
REGISTER sip:192.168.1.202 SIP/2.0
Via: SIP/2.0/UDP 192.168.1.40:36771;rport;branch=z9hG4bK-r-1
From: <sip:123001@192.168.1.202>;tag=reg-tag
To: <sip:123001@192.168.1.202>
Contact: <sip:123001@192.168.1.40:36771>
Call-ID: f00d-cafe-0001
CSeq: 8397 REGISTER
Expires: 300
Content-Length: 0

"""

DISCONNECT_DUMP_MSG = """\
17:33:50.888   pjsua_call.c  [DISCONNECTED] To: <sip:123001@192.168.1.202>;tag=dmdb421.o
\tCall time: 00h:00m:03s, 1st res in 100 ms, conn in 350ms
\t#0 audio PCMA @8kHz, sendrecv, peer=192.168.1.201:48556
\t   SRTP status: Not active
"""

NON_SIP_MSG = (
    "17:33:42.000   pjsua_app.c   Application started, sticking around\n"
    "Just a non-SIP info line"
)


class TestParseSipMetadata:
    def test_tx_invite_extracts_direction_method_cseq(self):
        md = parse_sip_metadata(TX_INVITE_MSG)
        assert md.direction == "TX"
        assert md.method == "INVITE"
        assert md.cseq == 8398

    def test_tx_invite_extracts_call_id(self):
        md = parse_sip_metadata(TX_INVITE_MSG)
        assert md.sip_call_id == "5d0cbc47-e4b5-4e7a-b868-5fb3138f367d"

    def test_tx_invite_extracts_from_to_uris(self):
        md = parse_sip_metadata(TX_INVITE_MSG)
        assert md.from_uri == "sip:123001@192.168.1.202"
        assert md.to_uri == "sip:123002@192.168.1.202"

    def test_from_to_with_bare_uri_no_angle_brackets(self):
        """Some SIP stacks emit `From: sip:user@host;tag=X` without `<>`.
        Parser must extract the bare URI form just like the bracketed one."""
        msg = (
            "12:00:00.000 pjsua_core.c TX 100 bytes Request msg OPTIONS/cseq=1 "
            "to UDP x:5060:\n"
            "OPTIONS sip:bob SIP/2.0\r\n"
            "From: sip:alice@example.com;tag=alice-tag\r\n"
            "To: sip:bob@example.com\r\n"
            "Call-ID: x\r\n"
            "CSeq: 1 OPTIONS\r\n"
            "\r\n"
        )
        md = parse_sip_metadata(msg)
        assert md.from_uri == "sip:alice@example.com"
        assert md.to_uri == "sip:bob@example.com"

    def test_from_to_with_display_name(self):
        msg = (
            "12:00:00.000 pjsua_core.c TX 100 bytes Request msg OPTIONS/cseq=1 "
            "to UDP x:5060:\n"
            "OPTIONS sip:bob SIP/2.0\r\n"
            'From: "Alice Smith" <sip:alice@example.com>;tag=alice\r\n'
            "To: <sip:bob@example.com>\r\n"
            "Call-ID: x\r\n"
            "CSeq: 1 OPTIONS\r\n"
            "\r\n"
        )
        md = parse_sip_metadata(msg)
        assert md.from_uri == "sip:alice@example.com"
        assert md.to_uri == "sip:bob@example.com"

    def test_tx_invite_extracts_via_ports(self):
        md = parse_sip_metadata(TX_INVITE_MSG)
        assert 36771 in md.via_ports

    def test_tx_invite_no_status_code(self):
        md = parse_sip_metadata(TX_INVITE_MSG)
        assert md.status_code is None

    def test_rx_response_extracts_status(self):
        md = parse_sip_metadata(RX_200_OK_MSG)
        assert md.direction == "RX"
        assert md.status_code == 200

    def test_rx_response_method_from_cseq(self):
        """For SIP responses, method comes from the CSeq header — the start
        line is `SIP/2.0 200 OK`, not `INVITE ...`."""
        md = parse_sip_metadata(RX_200_OK_MSG)
        assert md.method == "INVITE"
        assert md.cseq == 8398

    def test_rx_response_call_id(self):
        md = parse_sip_metadata(RX_200_OK_MSG)
        assert md.sip_call_id == "5d0cbc47-e4b5-4e7a-b868-5fb3138f367d"

    def test_register_method(self):
        md = parse_sip_metadata(TX_REGISTER_MSG)
        assert md.method == "REGISTER"
        assert md.from_uri == "sip:123001@192.168.1.202"
        assert md.sip_call_id == "f00d-cafe-0001"
        assert 36771 in md.via_ports

    def test_disconnect_dump_extracts_dump_remote_uri(self):
        md = parse_sip_metadata(DISCONNECT_DUMP_MSG)
        assert md.dump_remote_uri == "sip:123001@192.168.1.202"
        # No SIP message envelope → no direction/method
        assert md.direction is None
        assert md.method is None
        assert md.sip_call_id is None

    def test_non_sip_returns_empty_metadata(self):
        md = parse_sip_metadata(NON_SIP_MSG)
        assert md.direction is None
        assert md.method is None
        assert md.cseq is None
        assert md.status_code is None
        assert md.sip_call_id is None
        assert md.from_uri is None
        assert md.to_uri is None
        assert md.via_ports == ()
        assert md.dump_remote_uri is None

    def test_empty_string(self):
        md = parse_sip_metadata("")
        assert md.direction is None
        assert md.method is None
        assert md.sip_call_id is None


# ---------------------------------------------------------------------------
# filter_entries_by_owner — pure ownership-based filter that powers
# get_sip_log(phone_id=..., call_id=...). Tests use a mix of real-shape
# fixtures and minimal one-liners to cover each ownership signal.
# ---------------------------------------------------------------------------

# bob's TX 200 OK on his incoming-leg of alice→bob call. Same SIP Call-ID
# value as alice's INVITE? No — bob's incoming leg has its own Call-ID
# (the one carried in the RX INVITE that bob received). Plan-01 example:
# alice's outbound Call-ID = 5d0cbc47-... (set in TX_INVITE_MSG above).
# bob's incoming-leg Call-ID is the SAME — `From:`/`Call-ID:` are propagated
# verbatim through the B2BUA only on direct routing. With B2BUA transcoding
# (plan-01) bob sees a NEW Call-ID. We use distinct values here.
BOB_INCOMING_INVITE_MSG = """\
17:33:45.000   pjsua_core.c  RX 1100 bytes Request msg INVITE/cseq=582 (rdata0xb2b) from UDP 192.168.1.202:5060:
INVITE sip:123002@192.168.1.40 SIP/2.0
Via: SIP/2.0/UDP 192.168.1.202:5060;branch=z9hG4bK-bob-leg
Via: SIP/2.0/UDP 192.168.1.40:36772;branch=z9hG4bK-bob
From: <sip:123001@192.168.1.202>;tag=alice-tag
To: <sip:123002@192.168.1.202>
Call-ID: bob-side-call-uuid-9999
CSeq: 582 INVITE
Content-Length: 0

"""

BOB_DISCONNECT_DUMP_MSG = """\
17:33:50.999   pjsua_call.c  [DISCONNECTED] To: <sip:123001@192.168.1.202>;tag=bob-side
\tCall time: 00h:00m:03s
\t#0 audio PCMA @8kHz, sendrecv, peer=192.168.1.201:48556
"""


class TestFilterEntriesByOwner:
    """Ownership-based filter — primary mechanism for get_sip_log(phone_id=...).

    Signals (priority order):
      1. SIP Call-ID ∈ phone's known set (definitive)
      2. dump remote URI ∈ phone's known remote URIs (definitive for dumps)
      3. Via line carries phone's local transport port (REGISTER, ACK, etc.)
      4. Method=REGISTER and From URI matches phone's username
      5. Fallback: substring match on `sip:{username}@` (with warning)
    """

    def _alice(self) -> PhoneMeta:
        return PhoneMeta(
            phone_id="alice",
            username="123001",
            local_port=36771,
            sip_call_ids={"5d0cbc47-e4b5-4e7a-b868-5fb3138f367d", "f00d-cafe-0001"},
            remote_uris={"sip:123002@192.168.1.202"},
        )

    def _bob(self) -> PhoneMeta:
        return PhoneMeta(
            phone_id="bob",
            username="123002",
            local_port=36772,
            sip_call_ids={"bob-side-call-uuid-9999"},
            remote_uris={"sip:123001@192.168.1.202"},
        )

    def _phones(self) -> dict[str, PhoneMeta]:
        return {"alice": self._alice(), "bob": self._bob()}

    def test_no_filter_returns_all(self):
        entries = [{"msg": "any"}, {"msg": "another"}]
        kept, fallback = filter_entries_by_owner(entries, phones={}, target_phone=None)
        assert kept == entries
        assert fallback == 0

    def test_keeps_alice_invite_by_call_id(self):
        entries = [{"msg": TX_INVITE_MSG}]
        kept, fallback = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice"
        )
        assert len(kept) == 1
        assert fallback == 0

    def test_drops_bob_incoming_invite(self):
        """Bob's RX INVITE has `From: <sip:123001@>` — naive substring filter
        would falsely attribute it to alice. Structural check via Call-ID
        sees it belongs to bob's set, excludes it."""
        entries = [{"msg": BOB_INCOMING_INVITE_MSG}]
        kept, fallback = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice"
        )
        assert kept == []
        assert fallback == 0

    def test_drops_bob_disconnect_dump_with_alice_uri(self):
        """The CRITICAL false-positive from proposal-05: bob's [DISCONNECTED]
        dump contains `To: <sip:123001@>` (alice's URI, because she's bob's
        remote party). Substring filter mistakenly attributes the audio
        codec ("PCMA") to alice. Structural check via dump_remote_uri ∈
        bob's remote_uris correctly excludes."""
        entries = [{"msg": BOB_DISCONNECT_DUMP_MSG}]
        kept, fallback = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice"
        )
        assert kept == []
        assert fallback == 0

    def test_keeps_register_by_username_when_call_id_unknown(self):
        """REGISTER's Call-ID is not in any active call's set, but its
        From URI carries alice's username — owned by alice."""
        entries = [{"msg": TX_REGISTER_MSG}]
        # Drop the Call-ID from alice's set so REGISTER falls through to
        # username matching; the registration Call-ID is independent of
        # call dialogs and not always in the index.
        alice = PhoneMeta(
            phone_id="alice", username="123001", local_port=36771,
            sip_call_ids=set(), remote_uris=set(),
        )
        kept, fallback = filter_entries_by_owner(
            entries, phones={"alice": alice}, target_phone="alice"
        )
        assert len(kept) == 1
        assert fallback == 0

    def test_via_port_attribution(self):
        """A SIP message whose Via line carries alice's local transport
        port — even when Call-ID is unknown — attributes to alice."""
        msg = (
            "17:00:00.000 pjsua_core.c  TX 100 bytes Request msg OPTIONS/cseq=1 to UDP 1.1.1.1:5060:\n"
            "OPTIONS sip:1.1.1.1 SIP/2.0\n"
            "Via: SIP/2.0/UDP 192.168.1.40:36771;branch=z9hG4bK-x\n"
            "Call-ID: never-tracked\n"
            "CSeq: 1 OPTIONS\n\n"
        )
        kept, fallback = filter_entries_by_owner(
            [{"msg": msg}], phones=self._phones(), target_phone="alice"
        )
        assert len(kept) == 1
        assert fallback == 0

    def test_unknown_owner_falls_back_to_substring(self):
        """No structural signal but msg substring-matches alice's username.
        Kept and counted as fallback so caller can warn."""
        msg = "stray pjsua line referencing sip:123001@somewhere"
        kept, fallback = filter_entries_by_owner(
            [{"msg": msg}], phones=self._phones(), target_phone="alice"
        )
        assert len(kept) == 1
        assert fallback == 1

    def test_unknown_owner_no_substring_dropped(self):
        msg = "totally unrelated log line about something else"
        kept, fallback = filter_entries_by_owner(
            [{"msg": msg}], phones=self._phones(), target_phone="alice"
        )
        assert kept == []
        assert fallback == 0

    def test_dump_attribution_normalizes_uri_format(self):
        """pjsua2's `ci.remoteUri` returns `<sip:user@host>;tag=...` (or with
        a display name), while the `[DISCONNECTED] To: <X>` dump emits a
        bare `sip:user@host`. The filter must equate these formats —
        otherwise bob's dump leaks into alice's log even with a Phone
        tracker entry. Real failure observed in two-phone integration test."""
        bob = PhoneMeta(
            phone_id="bob", username="6002", local_port=5070,
            sip_call_ids=set(),
            remote_uris={"<sip:6001@172.20.0.2>;tag=alice-tag"},  # pjsua format
        )
        msg = (
            "  [DISCONNECTED] To: <sip:6001@172.20.0.2>;tag=bob-side\n"
            "    Call time: 00h:00m:00s\n"
            "    #0 audio PCMA @8kHz, sendrecv\n"
        )
        kept, _ = filter_entries_by_owner(
            [{"msg": msg}], phones={"bob": bob}, target_phone="bob"
        )
        assert len(kept) == 1, "bob's own dump should be attributed to bob"

        # And conversely — alice's filter must drop bob's dump.
        alice = PhoneMeta(
            phone_id="alice", username="6001", local_port=5060,
            sip_call_ids=set(),
            remote_uris={"<sip:6002@172.20.0.2>"},
        )
        kept_alice, _ = filter_entries_by_owner(
            [{"msg": msg}], phones={"alice": alice, "bob": bob},
            target_phone="alice",
        )
        assert kept_alice == [], (
            "bob's dump leaked into alice's log — URI normalization bug"
        )

    def test_method_filter(self):
        """method='INVITE' keeps only INVITE entries (case-insensitive)."""
        entries = [
            {"msg": TX_INVITE_MSG},
            {"msg": TX_REGISTER_MSG},
            {"msg": RX_200_OK_MSG},  # method=INVITE via CSeq
        ]
        kept, _ = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice", method="INVITE"
        )
        # TX INVITE + RX 200 OK (CSeq INVITE) — both have method=INVITE
        assert len(kept) == 2
        assert all("INVITE" in e["msg"] or "200" in e["msg"] for e in kept)

    def test_method_filter_case_insensitive(self):
        entries = [{"msg": TX_REGISTER_MSG}]
        kept, _ = filter_entries_by_owner(
            entries,
            phones={"alice": PhoneMeta(
                phone_id="alice", username="123001", local_port=36771,
                sip_call_ids=set(), remote_uris=set(),
            )},
            target_phone="alice",
            method="register",  # lowercase
        )
        assert len(kept) == 1

    def test_direction_filter_tx(self):
        entries = [
            {"msg": TX_INVITE_MSG},
            {"msg": RX_200_OK_MSG},
        ]
        kept, _ = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice", direction="TX"
        )
        assert len(kept) == 1
        assert "TX 1128 bytes" in kept[0]["msg"]

    def test_direction_filter_rx(self):
        entries = [
            {"msg": TX_INVITE_MSG},
            {"msg": RX_200_OK_MSG},
        ]
        kept, _ = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice", direction="RX"
        )
        assert len(kept) == 1
        assert "RX 956 bytes" in kept[0]["msg"]

    def test_status_code_filter(self):
        """status_code=200 keeps only responses with that exact status."""
        entries = [
            {"msg": TX_INVITE_MSG},     # request, no status
            {"msg": RX_200_OK_MSG},     # status=200
        ]
        kept, _ = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice", status_code=200
        )
        assert len(kept) == 1
        assert "200 OK" in kept[0]["msg"]

    def test_cseq_filter(self):
        """cseq=8398 keeps only entries on that specific transaction."""
        entries = [
            {"msg": TX_INVITE_MSG},      # cseq=8398
            {"msg": RX_200_OK_MSG},      # cseq=8398
            {"msg": TX_REGISTER_MSG},    # cseq=8397
        ]
        # Drop alice's sip_call_ids so REGISTER falls through to username match
        alice = PhoneMeta(
            phone_id="alice", username="123001", local_port=36771,
            sip_call_ids={"5d0cbc47-e4b5-4e7a-b868-5fb3138f367d"},
            remote_uris={"sip:123002@192.168.1.202"},
        )
        kept, _ = filter_entries_by_owner(
            entries, phones={"alice": alice}, target_phone="alice", cseq=8398
        )
        assert len(kept) == 2

    def test_method_and_direction_combined(self):
        entries = [
            {"msg": TX_INVITE_MSG},      # TX, INVITE
            {"msg": RX_200_OK_MSG},      # RX, INVITE (via CSeq)
            {"msg": TX_REGISTER_MSG},    # TX, REGISTER
        ]
        kept, _ = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice",
            method="INVITE", direction="TX",
        )
        assert len(kept) == 1
        assert "TX 1128 bytes Request msg INVITE" in kept[0]["msg"]

    def test_new_filters_are_optional(self):
        """All new filters default to None — entries pass through."""
        entries = [{"msg": TX_INVITE_MSG}]
        kept, _ = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice"
        )
        assert len(kept) == 1

    def test_method_filter_excludes_non_sip_entries(self):
        """A log line without a parseable SIP method is dropped when method
        filter is set — even if it would otherwise pass ownership."""
        msg_no_method = (
            "17:00:00.000 pjsua_core.c  Some random log line about sip:123001@x"
        )
        entries = [{"msg": msg_no_method}]
        kept, _ = filter_entries_by_owner(
            entries, phones=self._phones(), target_phone="alice", method="INVITE"
        )
        assert kept == []

    def test_call_id_filter_intersects(self):
        """target_sip_call_id is the SIP Call-ID string of the requested
        pjsua call — only entries whose Call-ID equals that string are kept,
        irrespective of phone-level owner."""
        entries = [
            {"msg": TX_INVITE_MSG},          # call-id 5d0cbc47-...
            {"msg": BOB_INCOMING_INVITE_MSG},  # call-id bob-side-...
            {"msg": TX_REGISTER_MSG},        # call-id f00d-cafe-0001
        ]
        kept, fallback = filter_entries_by_owner(
            entries,
            phones=self._phones(),
            target_phone="alice",
            target_sip_call_id="5d0cbc47-e4b5-4e7a-b868-5fb3138f367d",
        )
        assert len(kept) == 1
        assert "INVITE/cseq=8398" in kept[0]["msg"]


# ---------------------------------------------------------------------------
# parse_sdp_body — line-by-line SDP parser per RFC 4566. Output shape
# matches proposal-03's `sdp` field. Pure function, no external deps.
# ---------------------------------------------------------------------------

PLAN_01_SDP = (
    "v=0\r\n"
    "o=- 3986467525 3986467525 IN IP4 192.168.1.40\r\n"
    "s=pjmedia\r\n"
    "b=AS:84\r\n"
    "t=0 0\r\n"
    "a=X-nat:0\r\n"
    "m=audio 4000 RTP/AVP 0 120\r\n"
    "c=IN IP4 192.168.1.40\r\n"
    "b=TIAS:64000\r\n"
    "a=rtcp:4001 IN IP4 192.168.1.40\r\n"
    "a=sendrecv\r\n"
    "a=rtpmap:0 PCMU/8000\r\n"
    "a=rtpmap:120 telephone-event/8000\r\n"
    "a=fmtp:120 0-16\r\n"
)


class TestParseSdpBody:
    def test_full_sdp_extracts_envelope_fields(self):
        result = parse_sdp_body(PLAN_01_SDP)
        assert result is not None
        assert result["version"] == 0
        assert result["origin"] == {"username": "-", "ip": "192.168.1.40"}

    def test_full_sdp_one_audio_media(self):
        result = parse_sdp_body(PLAN_01_SDP)
        assert len(result["media"]) == 1
        media = result["media"][0]
        assert media["type"] == "audio"
        assert media["port"] == 4000
        assert media["protocol"] == "RTP/AVP"
        assert media["payload_types"] == [0, 120]
        assert media["direction"] == "sendrecv"
        assert media["rtcp_port"] == 4001

    def test_full_sdp_codecs_list(self):
        result = parse_sdp_body(PLAN_01_SDP)
        codecs = result["media"][0]["codecs"]
        assert codecs == [
            {"pt": 0, "name": "PCMU", "clock_rate": 8000},
            {"pt": 120, "name": "telephone-event", "clock_rate": 8000, "fmtp": "0-16"},
        ]

    def test_empty_returns_none(self):
        assert parse_sdp_body("") is None
        assert parse_sdp_body(None) is None

    def test_non_sdp_returns_none(self):
        """Plain text without a v=0 line — not SDP, return None."""
        assert parse_sdp_body("just plain text") is None
        assert parse_sdp_body("HTTP/1.1 200 OK\nContent-Type: text/html") is None

    def test_no_rtcp_line_yields_none(self):
        """rtcp_port is optional — when absent, field is None."""
        sdp = (
            "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n"
            "m=audio 5000 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
        result = parse_sdp_body(sdp)
        assert result["media"][0]["rtcp_port"] is None

    def test_default_direction_sendrecv(self):
        """Per RFC 3264, missing direction attribute defaults to sendrecv."""
        sdp = (
            "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n"
            "m=audio 5000 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
        result = parse_sdp_body(sdp)
        assert result["media"][0]["direction"] == "sendrecv"

    def test_hold_direction_sendonly(self):
        sdp = (
            "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n"
            "m=audio 5000 RTP/AVP 0\r\n"
            "a=sendonly\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
        result = parse_sdp_body(sdp)
        assert result["media"][0]["direction"] == "sendonly"

    def test_inactive_direction(self):
        sdp = (
            "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n"
            "m=audio 5000 RTP/AVP 0\r\n"
            "a=inactive\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
        )
        result = parse_sdp_body(sdp)
        assert result["media"][0]["direction"] == "inactive"

    def test_static_pt_without_rtpmap(self):
        """Static payload types (PT 0=PCMU, 8=PCMA, 9=G722) often appear
        without explicit a=rtpmap. We fill in canonical names per RFC 3551."""
        sdp = (
            "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n"
            "m=audio 5000 RTP/AVP 0 8 9\r\n"
        )
        result = parse_sdp_body(sdp)
        codecs = result["media"][0]["codecs"]
        codec_names = {(c["pt"], c["name"]) for c in codecs}
        assert (0, "PCMU") in codec_names
        assert (8, "PCMA") in codec_names
        assert (9, "G722") in codec_names

    def test_multi_section_audio_video(self):
        """SDP with both audio and video — both sections parsed independently."""
        sdp = (
            "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n"
            "m=audio 5000 RTP/AVP 0\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "m=video 5002 RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\n"
        )
        result = parse_sdp_body(sdp)
        assert len(result["media"]) == 2
        assert result["media"][0]["type"] == "audio"
        assert result["media"][0]["codecs"][0]["name"] == "PCMU"
        assert result["media"][1]["type"] == "video"
        assert result["media"][1]["codecs"][0]["name"] == "H264"
        assert result["media"][1]["codecs"][0]["clock_rate"] == 90000

    def test_lf_only_line_endings(self):
        """SDP with bare LF (not CRLF) still parses — pjlib normalises some
        inputs and some captures arrive without CRLF."""
        sdp = (
            "v=0\no=- 1 1 IN IP4 1.2.3.4\n"
            "m=audio 5000 RTP/AVP 0\na=rtpmap:0 PCMU/8000\n"
        )
        result = parse_sdp_body(sdp)
        assert result["version"] == 0
        assert result["media"][0]["codecs"][0]["name"] == "PCMU"

    def test_malformed_body_does_not_raise(self):
        """Garbage input returns None or partial dict, but never raises."""
        # Each of these used to crash a regex-based parser at one point or
        # another — exercise tolerance for surprising inputs.
        for bad in ["\x00\x01\x02", "v=0\nm=garbage", "v=0\no=incomplete"]:
            try:
                parse_sdp_body(bad)
            except Exception as e:
                pytest.fail(f"parse_sdp_body raised on {bad!r}: {e}")

    def test_dynamic_codec_with_fmtp(self):
        """Dynamic payload types (96..127) with fmtp params — common for
        opus, telephone-event, H264. fmtp value preserved as raw string."""
        sdp = (
            "v=0\r\no=- 1 1 IN IP4 1.2.3.4\r\n"
            "m=audio 5000 RTP/AVP 96\r\n"
            "a=rtpmap:96 opus/48000/2\r\n"
            "a=fmtp:96 minptime=10;useinbandfec=1\r\n"
        )
        result = parse_sdp_body(sdp)
        codec = result["media"][0]["codecs"][0]
        assert codec["pt"] == 96
        assert codec["name"] == "opus"
        assert codec["clock_rate"] == 48000
        assert codec["fmtp"] == "minptime=10;useinbandfec=1"


# ---------------------------------------------------------------------------
# parse_sip_headers — extract SIP message headers as dict[str, str | list].
# Multi-value headers (Via repeated through proxies, Route, Record-Route)
# collapse to a list so iteration order is stable.
# ---------------------------------------------------------------------------

INVITE_MESSAGE_TEXT = (
    "INVITE sip:123002@192.168.1.202 SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 192.168.1.40:36771;rport;branch=z9hG4bK-1\r\n"
    "Via: SIP/2.0/UDP 10.0.0.1:5060;branch=z9hG4bK-2\r\n"
    "Max-Forwards: 70\r\n"
    'From: "Alice" <sip:123001@192.168.1.202>;tag=alice-tag\r\n'
    "To: <sip:123002@192.168.1.202>\r\n"
    "Call-ID: 5d0cbc47-e4b5-4e7a-b868-5fb3138f367d\r\n"
    "CSeq: 8398 INVITE\r\n"
    "Contact: <sip:123001@192.168.1.40:36771>\r\n"
    "Content-Type: application/sdp\r\n"
    "Content-Length: 200\r\n"
    "\r\n"
    "v=0\r\n"
    "m=audio 4000 RTP/AVP 0\r\n"
)


class TestParseSipHeaders:
    def test_extracts_basic_headers(self):
        h = parse_sip_headers(INVITE_MESSAGE_TEXT)
        assert h["Max-Forwards"] == "70"
        assert h["Call-ID"] == "5d0cbc47-e4b5-4e7a-b868-5fb3138f367d"
        assert h["CSeq"] == "8398 INVITE"
        assert h["Content-Type"] == "application/sdp"
        assert h["Content-Length"] == "200"

    def test_multi_value_via_collapsed_to_list(self):
        h = parse_sip_headers(INVITE_MESSAGE_TEXT)
        via = h["Via"]
        assert isinstance(via, list)
        assert len(via) == 2
        assert "192.168.1.40:36771" in via[0]
        assert "10.0.0.1:5060" in via[1]

    def test_from_to_with_display_name(self):
        h = parse_sip_headers(INVITE_MESSAGE_TEXT)
        assert h["From"] == '"Alice" <sip:123001@192.168.1.202>;tag=alice-tag'
        assert h["To"] == "<sip:123002@192.168.1.202>"

    def test_skips_start_line(self):
        h = parse_sip_headers(INVITE_MESSAGE_TEXT)
        assert "INVITE" not in h
        assert "INVITE sip:" not in h

    def test_response_start_line_skipped(self):
        msg = (
            "SIP/2.0 200 OK\r\n"
            "Via: SIP/2.0/UDP 1.2.3.4\r\n"
            "Call-ID: x\r\n"
            "\r\n"
        )
        h = parse_sip_headers(msg)
        assert "SIP/2.0" not in h
        assert h["Call-ID"] == "x"

    def test_body_excluded(self):
        h = parse_sip_headers(INVITE_MESSAGE_TEXT)
        # Body field "v=0" must not be picked up as a header
        assert "v" not in h

    def test_no_body_separator(self):
        msg = (
            "INVITE sip:x@y SIP/2.0\r\n"
            "Call-ID: x\r\n"
            "Content-Length: 0\r\n"
        )
        h = parse_sip_headers(msg)
        assert h["Call-ID"] == "x"
        assert h["Content-Length"] == "0"

    def test_lf_only_line_endings(self):
        msg = (
            "INVITE sip:x@y SIP/2.0\n"
            "Call-ID: x\n"
            "\n"
        )
        h = parse_sip_headers(msg)
        assert h["Call-ID"] == "x"

    def test_empty_input_returns_empty_dict(self):
        assert parse_sip_headers("") == {}
        assert parse_sip_headers(None) == {}

    def test_malformed_lines_skipped(self):
        """Lines without a colon are not headers — skip silently."""
        msg = (
            "INVITE sip:x@y SIP/2.0\r\n"
            "garbage line without colon\r\n"
            "Call-ID: x\r\n"
            "\r\n"
        )
        h = parse_sip_headers(msg)
        assert h["Call-ID"] == "x"
        assert "garbage line without colon" not in h

    def test_canonical_case_for_well_known_headers(self):
        """Header names normalize to canonical case (Call-ID, Content-Type
        etc.) so consumers can use stable keys regardless of the casing
        the wire uses."""
        msg = (
            "INVITE sip:x@y SIP/2.0\r\n"
            "call-id: lowercased\r\n"
            "CONTENT-LENGTH: 0\r\n"
            "\r\n"
        )
        h = parse_sip_headers(msg)
        assert h["Call-ID"] == "lowercased"
        assert h["Content-Length"] == "0"


# ---------------------------------------------------------------------------
# structurize_message — composes parse_sip_metadata + parse_sip_headers +
# parse_sdp_body, plus a small timestamp regex, into proposal-03's
# top-level message dict.
# ---------------------------------------------------------------------------

class TestStructurizeMessage:
    def test_tx_invite_full_shape(self):
        entry = {"msg": TX_INVITE_MSG, "level": 4, "thread": "x"}
        result = structurize_message(entry)
        assert result is not None
        assert result["ts"] == "17:33:44.234"
        assert result["direction"] == "TX"
        assert result["method"] == "INVITE"
        assert result["cseq"] == 8398
        assert result["call_id"] == "5d0cbc47-e4b5-4e7a-b868-5fb3138f367d"
        assert result["from"] == "sip:123001@192.168.1.202"
        assert result["to"] == "sip:123002@192.168.1.202"

    def test_tx_invite_includes_headers(self):
        entry = {"msg": TX_INVITE_MSG}
        result = structurize_message(entry)
        h = result["headers"]
        assert h["Call-ID"] == "5d0cbc47-e4b5-4e7a-b868-5fb3138f367d"
        assert h["CSeq"] == "8398 INVITE"
        assert h["Content-Type"] == "application/sdp"

    def test_tx_invite_parses_sdp(self):
        entry = {"msg": TX_INVITE_MSG}
        result = structurize_message(entry)
        sdp = result["sdp"]
        assert sdp is not None
        codec_names = {c["name"] for c in sdp["media"][0]["codecs"]}
        assert "PCMU" in codec_names
        assert "telephone-event" in codec_names
        # No PCMA leak — proposal-05/03 acceptance criterion intersection
        assert "PCMA" not in codec_names

    def test_rx_response_includes_status(self):
        entry = {"msg": RX_200_OK_MSG}
        result = structurize_message(entry)
        assert result["direction"] == "RX"
        assert result["status_code"] == 200
        assert result["method"] == "INVITE"  # resolved via CSeq header

    def test_register_has_no_sdp(self):
        """REGISTER carries no SDP body — sdp field is None."""
        entry = {"msg": TX_REGISTER_MSG}
        result = structurize_message(entry)
        assert result["method"] == "REGISTER"
        assert result["sdp"] is None

    def test_non_sip_returns_none(self):
        """Plain pjlib log lines (not SIP messages) return None — caller
        drops them from the structured-message list."""
        entry = {"msg": "17:00:00.000 pjsua_core.c  Library destroyed"}
        assert structurize_message(entry) is None

    def test_disconnect_dump_returns_none(self):
        """[DISCONNECTED] pjsua dump is not a SIP message — drop it."""
        entry = {"msg": DISCONNECT_DUMP_MSG}
        assert structurize_message(entry) is None

    def test_timestamp_extracted(self):
        msg = (
            "12:34:56.789 pjsua_core.c TX 100 bytes Request msg OPTIONS/cseq=1 "
            "to UDP 1.2.3.4:5060:\n"
            "OPTIONS sip:x SIP/2.0\r\n"
            "Call-ID: y\r\n"
            "CSeq: 1 OPTIONS\r\n"
            "\r\n"
        )
        result = structurize_message({"msg": msg})
        assert result["ts"] == "12:34:56.789"

    def test_empty_msg_returns_none(self):
        assert structurize_message({"msg": ""}) is None
        assert structurize_message({}) is None
