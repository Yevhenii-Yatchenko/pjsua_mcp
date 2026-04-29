"""Pcap → RTP/RTCP flow analyser.

Reads a pcap file (any of the link types `tcpdump -i any` produces — DLT
EN10MB, LINUX_SLL, LINUX_SLL2 — plus DLT_NULL for synthesised test
fixtures), decodes IPv4/UDP, and classifies each UDP payload as RTP,
RTCP, or neither based on the RFC 3550 framing in the first two bytes.

Returns aggregated per-flow counts plus an optional per-phone summary
when `phone_rtp_port` is known: which RTP codecs were observed on the
phone's local RTP port, and whether any "foreign" codecs leaked in
(used by the per-phone SDP filter conformance test).
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

import dpkt

log = logging.getLogger(__name__)


# DLT constants (libpcap) — dpkt exposes the EN10MB and NULL ones, but
# the SLL/SLL2 values come from `tcpdump -i any` and aren't named in dpkt.
_LINKTYPE_NAMES = {
    0: "NULL",
    1: "EN10MB",
    113: "LINUX_SLL",
    276: "LINUX_SLL2",
    12: "RAW",
}

# RFC 3551 §6 static payload-type → codec name (audio + telephone-event
# falls in the dynamic 96-127 range, but PortaSIP+pjsua usually settles
# on 120 for telephone-event by negotiation, so map that too).
_PT_TO_CODEC = {
    0: "PCMU",
    3: "GSM",
    4: "G723",
    5: "DVI4",
    6: "DVI4",
    7: "LPC",
    8: "PCMA",
    9: "G722",
    10: "L16",
    11: "L16",
    12: "QCELP",
    13: "CN",
    14: "MPA",
    15: "G728",
    16: "DVI4",
    17: "DVI4",
    18: "G729",
    25: "CelB",
    26: "JPEG",
    28: "nv",
    31: "H261",
    32: "MPV",
    33: "MP2T",
    34: "H263",
    96: "dynamic-96",
    101: "telephone-event",
    120: "telephone-event",
}

# RTCP packet type → human name (RFC 3550 §6, RFC 4585 for FB).
_RTCP_PT_NAMES = {
    200: "SR",
    201: "RR",
    202: "SDES",
    203: "BYE",
    204: "APP",
    205: "RTPFB",
    206: "PSFB",
}


def _strip_link(buf: bytes, linktype: int) -> bytes | None:
    """Return the IP layer bytes from one pcap frame, or None if not IPv4
    or unsupported framing.
    """
    if linktype == dpkt.pcap.DLT_NULL:
        # 4-byte family header, e.g. AF_INET=2 little-endian on Linux.
        if len(buf) < 4:
            return None
        return buf[4:]
    if linktype == dpkt.pcap.DLT_EN10MB:
        try:
            eth = dpkt.ethernet.Ethernet(buf)
        except Exception:
            return None
        if not isinstance(eth.data, dpkt.ip.IP):
            return None
        return bytes(eth.data)
    if linktype == 113:  # DLT_LINUX_SLL — `tcpdump -i any` (older kernels)
        if len(buf) < 16:
            return None
        return buf[16:]
    if linktype == 276:  # DLT_LINUX_SLL2 — `tcpdump -i any` (newer kernels)
        if len(buf) < 20:
            return None
        return buf[20:]
    if linktype == 12:  # DLT_RAW — pure IP
        return buf
    return None


def _classify(payload: bytes) -> tuple[str, int] | None:
    """Classify a UDP payload as ('rtp', pt) or ('rtcp', pt), or None.

    RFC 3550: first byte top 2 bits = version 2 (0x80…0xBF) means RTP or
    RTCP. Second byte:
      - 200..204 (and 205,206 from RFC 4585) → RTCP (return raw PT).
      - else → RTP, mask out the marker bit (return PT 0..127).

    RTCP needs only a 4-byte header (RFC 3550 §6.1); RTP needs the full
    12-byte header. Apply the right minimum per kind.
    """
    if len(payload) < 4:
        return None
    if (payload[0] & 0xC0) != 0x80:
        return None
    pt_raw = payload[1]
    if pt_raw in _RTCP_PT_NAMES:
        return ("rtcp", pt_raw)
    if len(payload) < 12:
        return None
    return ("rtp", pt_raw & 0x7F)


def _codec_name(pt: int) -> str:
    return _PT_TO_CODEC.get(pt, f"PT={pt}")


def analyze_pcap(
    path: str | Path,
    phone_rtp_port: int | None = None,
    expected_codecs: list[str] | None = None,
) -> dict[str, Any]:
    """Walk `path` and return aggregated RTP/RTCP flow counts.

    Args:
        path: pcap file path. May be empty (zero packets) or unreadable —
            both return populated dicts with `error` set rather than
            raising.
        phone_rtp_port: this phone's local RTP UDP port, used to compute
            the per-phone summary fields (`phone_rtp_codecs_seen`,
            `non_phone_codecs_on_phone_port`). When None, those fields
            are left None.
        expected_codecs: codec names the phone is configured to use.
            If provided alongside `phone_rtp_port`, codecs seen on the
            phone's port that are NOT in this list become
            `non_phone_codecs_on_phone_port` (a non-empty list signals
            the per-phone SDP filter is leaking). When None,
            `non_phone_codecs_on_phone_port` is reported as `[]` and is
            informational only.

    Result shape — see `proposals/01-analyze-capture.md` for the spec.
    """
    p = Path(path)
    result: dict[str, Any] = {
        "path": str(p),
        "linktype": None,
        "linktype_name": None,
        "total_packets": 0,
        "rtp_flows": [],
        "rtcp_flows": [],
        "phone_rtp_port": phone_rtp_port,
        "phone_rtp_codecs_seen": None,
        "non_phone_codecs_on_phone_port": None,
        "error": None,
    }

    if not p.exists():
        result["error"] = f"pcap not found: {p}"
        return result

    rtp_counts: Counter[tuple[int, int, int]] = Counter()
    rtcp_counts: Counter[tuple[int, int, int]] = Counter()
    total = 0

    try:
        with p.open("rb") as f:
            try:
                reader = dpkt.pcap.Reader(f)
            except Exception as exc:
                result["error"] = f"unreadable pcap: {exc}"
                return result

            linktype = reader.datalink()
            result["linktype"] = linktype
            result["linktype_name"] = _LINKTYPE_NAMES.get(linktype)

            if linktype not in _LINKTYPE_NAMES:
                result["error"] = f"unknown linktype {linktype}"
                return result

            for _, buf in reader:
                total += 1
                # Per-packet try/except — tcpdump pcaps can include IPv6
                # discovery, ARP, or otherwise malformed frames the IPv4
                # decoder will reject. Skip them silently.
                try:
                    ip_buf = _strip_link(buf, linktype)
                    if ip_buf is None:
                        continue
                    ip = dpkt.ip.IP(ip_buf)
                except Exception:
                    continue
                if not isinstance(ip.data, dpkt.udp.UDP):
                    continue
                udp = ip.data
                klass = _classify(udp.data)
                if klass is None:
                    continue
                kind, pt = klass
                key = (int(udp.sport), int(udp.dport), pt)
                if kind == "rtp":
                    rtp_counts[key] += 1
                else:
                    rtcp_counts[key] += 1
    except OSError as exc:
        result["error"] = f"cannot read pcap: {exc}"
        return result

    result["total_packets"] = total

    rtp_flows = [
        {
            "src_port": s,
            "dst_port": d,
            "payload_type": pt,
            "codec": _codec_name(pt),
            "count": cnt,
        }
        for (s, d, pt), cnt in sorted(rtp_counts.items())
    ]
    rtcp_flows = [
        {
            "src_port": s,
            "dst_port": d,
            "payload_type": pt,
            "name": _RTCP_PT_NAMES.get(pt, f"PT={pt}"),
            "count": cnt,
        }
        for (s, d, pt), cnt in sorted(rtcp_counts.items())
    ]
    result["rtp_flows"] = rtp_flows
    result["rtcp_flows"] = rtcp_flows

    if phone_rtp_port is not None:
        seen_pt: list[str] = []
        for flow in rtp_flows:
            if flow["src_port"] != phone_rtp_port and flow["dst_port"] != phone_rtp_port:
                continue
            codec = flow["codec"]
            if codec not in seen_pt:
                seen_pt.append(codec)
        result["phone_rtp_codecs_seen"] = seen_pt
        if expected_codecs is not None:
            allowed = {c.upper() for c in expected_codecs}
            result["non_phone_codecs_on_phone_port"] = [
                c for c in seen_pt if c.upper() not in allowed
            ]
        else:
            result["non_phone_codecs_on_phone_port"] = []

    return result
