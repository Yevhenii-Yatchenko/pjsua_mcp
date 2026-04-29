"""Unit tests for src.pcap_analyzer.analyze_pcap.

Two flavours:
  * synthesised pcaps (dpkt writer) for linktype/RTP/RTCP edge cases —
    no test fixture file, fully self-contained;
  * real plan-01 pcaps captured by `tcpdump -i any` on a live PortaSIP
    setup, stored under `tests/fixtures/`. These verify the analyser
    against the proposal's acceptance criteria.
"""

from __future__ import annotations

import struct
from pathlib import Path

import dpkt
import pytest

from src.pcap_analyzer import analyze_pcap


FIXTURES = Path(__file__).parent / "fixtures"
ALICE_PCAP = FIXTURES / "plan_01_alice.pcap"
BOB_PCAP = FIXTURES / "plan_01_bob.pcap"


# ---------------------------------------------------------------------------
# Synth helpers — write minimal valid pcaps with a chosen linktype.
# ---------------------------------------------------------------------------
def _rtp_packet(pt: int, marker: bool = False) -> bytes:
    """12-byte RTP header (V=2) + 20-byte zero payload."""
    byte0 = 0x80
    byte1 = (0x80 if marker else 0x00) | (pt & 0x7F)
    return bytes([byte0, byte1]) + b"\x00" * 10 + b"\x00" * 20


def _rtcp_packet(pt: int) -> bytes:
    """Minimal RTCP packet — V=2, RC=0, length=1, raw PT (e.g. 200)."""
    # byte0: V=2, P=0, RC=0  → 0x80
    # byte1: PT (200..204)
    # bytes 2-3: length in 32-bit words (1 = header only, no body)
    return bytes([0x80, pt & 0xFF, 0x00, 0x01]) + b"\x00" * 4


def _ip_udp(payload: bytes, sport: int = 4000, dport: int = 51124) -> bytes:
    ip = dpkt.ip.IP(
        src=b"\x7f\x00\x00\x01",
        dst=b"\x7f\x00\x00\x02",
        p=dpkt.ip.IP_PROTO_UDP,
    )
    ip.data = dpkt.udp.UDP(sport=sport, dport=dport, data=payload)
    ip.len = len(bytes(ip))
    return bytes(ip)


def _write_null_pcap(path: Path, packets: list[tuple[bytes, int, int]]) -> None:
    """DLT_NULL pcap (linktype 0) — 4-byte family header (AF_INET=2 LE)."""
    with path.open("wb") as f:
        writer = dpkt.pcap.Writer(f, linktype=dpkt.pcap.DLT_NULL)
        link = struct.pack("<I", 2)
        for payload, sport, dport in packets:
            writer.writepkt(link + _ip_udp(payload, sport, dport), ts=0.0)


def _write_en10mb_pcap(path: Path, packets: list[tuple[bytes, int, int]]) -> None:
    """DLT_EN10MB pcap (linktype 1) — full Ethernet framing."""
    with path.open("wb") as f:
        writer = dpkt.pcap.Writer(f, linktype=dpkt.pcap.DLT_EN10MB)
        for payload, sport, dport in packets:
            eth = dpkt.ethernet.Ethernet(
                src=b"\x00\x00\x00\x00\x00\x01",
                dst=b"\x00\x00\x00\x00\x00\x02",
                type=dpkt.ethernet.ETH_TYPE_IP,
            )
            eth.data = dpkt.ip.IP(
                src=b"\x7f\x00\x00\x01",
                dst=b"\x7f\x00\x00\x02",
                p=dpkt.ip.IP_PROTO_UDP,
            )
            eth.data.data = dpkt.udp.UDP(sport=sport, dport=dport, data=payload)
            eth.data.len = len(bytes(eth.data))
            writer.writepkt(bytes(eth), ts=0.0)


def _write_sll_pcap(path: Path, linktype: int, packets: list[tuple[bytes, int, int]]) -> None:
    """LINUX_SLL (113) or LINUX_SLL2 (276) pcap — write the pcap-savefile
    header by hand because dpkt doesn't accept either as a known DLT."""
    pcap_magic = b"\xd4\xc3\xb2\xa1"  # little-endian, microsecond
    snaplen = 65535
    header = struct.pack(
        "<IHHiIII",
        int.from_bytes(pcap_magic, "little"),
        2, 4,                        # version major/minor
        0, 0,                         # thiszone, sigfigs
        snaplen, linktype,
    )
    with path.open("wb") as f:
        f.write(header)
        for payload, sport, dport in packets:
            ip_bytes = _ip_udp(payload, sport, dport)
            if linktype == 113:
                # LINUX_SLL: 16-byte cooked header
                link_hdr = struct.pack(">HHHQH", 0, 1, 6, 0, 0x0800)
            else:  # 276 = LINUX_SLL2
                # LINUX_SLL2: 20-byte cooked header (newer kernels)
                # protocol(BE 2), reserved(2), interface(BE 4),
                # ARPHRD_type(BE 2), packet_type(1), addr_len(1),
                # link_addr(8). dpkt ignores the bytes — we only need
                # the right total length so the parser strips correctly.
                link_hdr = struct.pack(">HHIHBB", 0x0800, 0, 1, 1, 0, 6) + b"\x00" * 8
            full = link_hdr + ip_bytes
            ts_sec, ts_usec = 0, 0
            f.write(struct.pack("<IIII", ts_sec, ts_usec, len(full), len(full)))
            f.write(full)


# ---------------------------------------------------------------------------
# Edge cases — synthesised pcaps
# ---------------------------------------------------------------------------
class TestLinktypes:
    def test_empty_pcap_returns_empty_flows(self, tmp_path):
        p = tmp_path / "empty.pcap"
        _write_null_pcap(p, [])
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["total_packets"] == 0
        assert out["rtp_flows"] == []
        assert out["rtcp_flows"] == []

    def test_dlt_null_linktype_extracts_rtp(self, tmp_path):
        p = tmp_path / "null.pcap"
        _write_null_pcap(p, [(_rtp_packet(0), 4000, 51124)] * 5)
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["linktype"] == 0
        assert out["linktype_name"] == "NULL"
        assert out["total_packets"] == 5
        assert out["rtp_flows"] == [
            {"src_port": 4000, "dst_port": 51124,
             "payload_type": 0, "codec": "PCMU", "count": 5},
        ]
        assert out["rtcp_flows"] == []

    def test_dlt_en10mb_linktype_extracts_rtp(self, tmp_path):
        p = tmp_path / "eth.pcap"
        _write_en10mb_pcap(p, [(_rtp_packet(8), 4002, 48556)] * 3)
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["linktype"] == 1
        assert out["linktype_name"] == "EN10MB"
        assert out["total_packets"] == 3
        assert out["rtp_flows"] == [
            {"src_port": 4002, "dst_port": 48556,
             "payload_type": 8, "codec": "PCMA", "count": 3},
        ]

    def test_dlt_linux_sll_extracts_rtp(self, tmp_path):
        p = tmp_path / "sll.pcap"
        _write_sll_pcap(p, 113, [(_rtp_packet(9), 4000, 51124)] * 2)
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["linktype"] == 113
        assert out["linktype_name"] == "LINUX_SLL"
        assert out["rtp_flows"][0]["codec"] == "G722"

    def test_dlt_linux_sll2_extracts_rtp(self, tmp_path):
        p = tmp_path / "sll2.pcap"
        _write_sll_pcap(p, 276, [(_rtp_packet(0), 4000, 51124)] * 4)
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["linktype"] == 276
        assert out["linktype_name"] == "LINUX_SLL2"
        assert out["total_packets"] == 4
        assert out["rtp_flows"][0]["codec"] == "PCMU"

    def test_unknown_linktype_returns_error(self, tmp_path):
        """Linktype not in the known table yields an `error` field, not
        an exception, so the MCP tool can surface it cleanly."""
        p = tmp_path / "bad.pcap"
        # Linktype 999 — clearly bogus
        pcap_magic = b"\xd4\xc3\xb2\xa1"
        header = struct.pack(
            "<IHHiIII",
            int.from_bytes(pcap_magic, "little"),
            2, 4, 0, 0, 65535, 999,
        )
        p.write_bytes(header)
        out = analyze_pcap(p)
        assert out["error"] == "unknown linktype 999"
        assert out["linktype"] == 999
        assert out["linktype_name"] is None

    def test_missing_file_returns_error(self, tmp_path):
        out = analyze_pcap(tmp_path / "does_not_exist.pcap")
        assert out["error"] is not None
        assert "not found" in out["error"]
        assert out["total_packets"] == 0
        assert out["rtp_flows"] == []


class TestRtpRtcpClassification:
    def test_rtcp_classified_separately(self, tmp_path):
        """RTCP packets (PT 200..204) must NOT show up in rtp_flows even
        though they share the V=2 first-byte pattern with RTP."""
        p = tmp_path / "mix.pcap"
        _write_null_pcap(p, [
            (_rtp_packet(0), 4000, 51124),     # RTP PCMU
            (_rtcp_packet(200), 4001, 51125),  # RTCP SR
            (_rtcp_packet(201), 4001, 51125),  # RTCP RR
            (_rtp_packet(0), 4000, 51124),
        ])
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["total_packets"] == 4
        assert len(out["rtp_flows"]) == 1
        assert out["rtp_flows"][0]["count"] == 2
        assert {f["payload_type"] for f in out["rtcp_flows"]} == {200, 201}
        assert {f["name"] for f in out["rtcp_flows"]} == {"SR", "RR"}

    def test_non_rtp_udp_is_ignored(self, tmp_path):
        """SIP / DNS / arbitrary UDP without the V=2 first-byte must be
        ignored entirely (no rtp_flows, no rtcp_flows)."""
        p = tmp_path / "sip.pcap"
        _write_null_pcap(p, [
            (b"REGISTER sip:x SIP/2.0\r\n" + b"\x00" * 10, 5060, 5060),
        ])
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["total_packets"] == 1
        assert out["rtp_flows"] == []
        assert out["rtcp_flows"] == []

    def test_short_udp_payload_ignored(self, tmp_path):
        """Payload shorter than 12 bytes can't be RTP — ignore."""
        p = tmp_path / "short.pcap"
        _write_null_pcap(p, [(b"\x80\x00abc", 4000, 51124)])
        out = analyze_pcap(p)
        assert out["error"] is None
        assert out["rtp_flows"] == []

    def test_marker_bit_ignored_in_pt_extraction(self, tmp_path):
        """RTP marker bit (top of byte 1) must be masked out — PT field
        is only 7 bits and a marker-set frame for PCMU should still
        report PT=0, not PT=128."""
        p = tmp_path / "mark.pcap"
        _write_null_pcap(p, [(_rtp_packet(0, marker=True), 4000, 51124)])
        out = analyze_pcap(p)
        assert out["rtp_flows"] == [
            {"src_port": 4000, "dst_port": 51124,
             "payload_type": 0, "codec": "PCMU", "count": 1},
        ]


class TestPerPhoneSummary:
    def test_phone_rtp_port_filters_codecs_seen(self, tmp_path):
        p = tmp_path / "mixed.pcap"
        _write_null_pcap(p, [
            (_rtp_packet(0), 4000, 51124),    # alice's port — PCMU
            (_rtp_packet(0), 51124, 4000),    # alice's port — PCMU (reverse)
            (_rtp_packet(8), 4002, 48556),    # bob's port — PCMA (irrelevant)
        ])
        out = analyze_pcap(p, phone_rtp_port=4000)
        assert out["phone_rtp_port"] == 4000
        assert out["phone_rtp_codecs_seen"] == ["PCMU"]
        # No expected_codecs → just informational, empty by definition.
        assert out["non_phone_codecs_on_phone_port"] == []

    def test_expected_codecs_flags_leaks(self, tmp_path):
        """When expected_codecs=[PCMU] but PCMA also flows on the
        phone's port, PCMA appears in non_phone_codecs_on_phone_port."""
        p = tmp_path / "leak.pcap"
        _write_null_pcap(p, [
            (_rtp_packet(0), 4000, 51124),    # PCMU on phone's port
            (_rtp_packet(8), 4000, 51124),    # PCMA leaked onto phone's port
        ])
        out = analyze_pcap(p, phone_rtp_port=4000, expected_codecs=["PCMU"])
        assert sorted(out["phone_rtp_codecs_seen"]) == ["PCMA", "PCMU"]
        assert out["non_phone_codecs_on_phone_port"] == ["PCMA"]

    def test_no_phone_port_leaves_summary_none(self, tmp_path):
        p = tmp_path / "x.pcap"
        _write_null_pcap(p, [(_rtp_packet(0), 4000, 51124)])
        out = analyze_pcap(p)
        assert out["phone_rtp_port"] is None
        assert out["phone_rtp_codecs_seen"] is None
        assert out["non_phone_codecs_on_phone_port"] is None


# ---------------------------------------------------------------------------
# Real fixtures — the proposal's acceptance criteria
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not ALICE_PCAP.exists(),
                    reason="plan-01 alice.pcap fixture not present")
class TestPlan01Alice:
    def test_linktype_is_sll2(self):
        out = analyze_pcap(ALICE_PCAP)
        assert out["linktype"] == 276
        assert out["linktype_name"] == "LINUX_SLL2"

    def test_total_packets_meets_acceptance_floor(self):
        out = analyze_pcap(ALICE_PCAP)
        # Acceptance criterion 5: total_packets >= 343 on the alice fixture
        assert out["total_packets"] >= 343

    def test_phone_4000_only_uses_pcmu(self):
        out = analyze_pcap(
            ALICE_PCAP,
            phone_rtp_port=4000,
            expected_codecs=["PCMU"],
        )
        # Acceptance criterion 1
        assert out["phone_rtp_codecs_seen"] == ["PCMU"]
        assert out["non_phone_codecs_on_phone_port"] == []

    def test_rtp_and_rtcp_flows_both_non_empty(self):
        out = analyze_pcap(ALICE_PCAP)
        assert out["error"] is None
        # tcpdump -i any captures both directions on the same host, so
        # we should see at least the alice-side PCMU pair plus some
        # RTCP traffic on RTP+1.
        port_4000_flows = [f for f in out["rtp_flows"]
                           if 4000 in (f["src_port"], f["dst_port"])]
        assert any(f["codec"] == "PCMU" for f in port_4000_flows)
        assert len(out["rtcp_flows"]) >= 1


@pytest.mark.skipif(not BOB_PCAP.exists(),
                    reason="plan-01 bob.pcap fixture not present")
class TestPlan01Bob:
    def test_phone_4002_only_uses_pcma(self):
        out = analyze_pcap(
            BOB_PCAP,
            phone_rtp_port=4002,
            expected_codecs=["PCMA"],
        )
        # Acceptance criterion 2
        assert out["phone_rtp_codecs_seen"] == ["PCMA"]
        assert out["non_phone_codecs_on_phone_port"] == []
