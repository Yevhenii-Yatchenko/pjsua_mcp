"""Unit tests for the pcap → RTP payload-type extractor."""

from __future__ import annotations

import struct

import pytest

from tests._rtp_helpers import rtp_payload_types_in_pcap


def _make_pcap_with_rtp(pts_outgoing: list[int], path) -> None:
    """Synthesise a tiny pcap with N RTP packets, each carrying the
    given payload-type byte. Uses DLT_NULL (loopback) — minimal framing."""
    import dpkt

    with open(path, "wb") as f:
        writer = dpkt.pcap.Writer(f, linktype=dpkt.pcap.DLT_NULL)
        for pt in pts_outgoing:
            link = struct.pack("<I", 2)  # AF_INET=2 little-endian
            ip = dpkt.ip.IP(
                src=b"\x7f\x00\x00\x01",
                dst=b"\x7f\x00\x00\x02",
                p=dpkt.ip.IP_PROTO_UDP,
            )
            ip.data = dpkt.udp.UDP(
                sport=4000, dport=4002,
                data=bytes([0x80, pt & 0x7F])
                + b"\x00" * 10  # rest of 12-byte RTP header
                + b"\x00" * 20,  # fake payload
            )
            ip.len = len(bytes(ip))
            writer.writepkt(link + bytes(ip), ts=0.0)


class TestRtpPayloadTypes:
    def test_extracts_single_pt(self, tmp_path):
        p = tmp_path / "x.pcap"
        _make_pcap_with_rtp([8] * 5, p)
        assert rtp_payload_types_in_pcap(p) == {8}

    def test_extracts_multiple_pts(self, tmp_path):
        p = tmp_path / "x.pcap"
        _make_pcap_with_rtp([0, 0, 8, 8, 0], p)
        assert rtp_payload_types_in_pcap(p) == {0, 8}

    def test_empty_pcap(self, tmp_path):
        p = tmp_path / "x.pcap"
        _make_pcap_with_rtp([], p)
        assert rtp_payload_types_in_pcap(p) == set()

    def test_skips_non_rtp(self, tmp_path):
        """A UDP packet whose first byte isn't 0x80 (RTP v2, no padding,
        no extension, CSRC=0) must be ignored — could be DNS, SIP, etc."""
        p = tmp_path / "x.pcap"
        import dpkt
        with open(p, "wb") as f:
            writer = dpkt.pcap.Writer(f, linktype=dpkt.pcap.DLT_NULL)
            link = struct.pack("<I", 2)
            ip = dpkt.ip.IP(
                src=b"\x7f\x00\x00\x01",
                dst=b"\x7f\x00\x00\x02",
                p=dpkt.ip.IP_PROTO_UDP,
            )
            ip.data = dpkt.udp.UDP(
                sport=5060, dport=5060,
                data=b"REGISTER sip:x SIP/2.0\r\n",
            )
            ip.len = len(bytes(ip))
            writer.writepkt(link + bytes(ip), ts=0.0)
        assert rtp_payload_types_in_pcap(p) == set()
