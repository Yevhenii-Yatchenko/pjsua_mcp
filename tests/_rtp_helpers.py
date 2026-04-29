"""Test-only pcap → RTP payload-type set extractor.

Reads the pcap (any DLT — `-i any` on tcpdump produces DLT_LINUX_SLL),
decodes Ethernet/IP/UDP, and inspects each UDP payload's first two
bytes for an RTP v2 header (0x80...0x8F at byte 0, no padding, no
extension, no CSRC). Returns the set of distinct payload types seen.

This is for assertion shape:
    assert rtp_payload_types_in_pcap(p) == {8}
where 8 = PCMA (RFC 3551 static table).
"""

from __future__ import annotations

import dpkt


def rtp_payload_types_in_pcap(path) -> set[int]:
    pts: set[int] = set()
    with open(path, "rb") as f:
        try:
            reader = dpkt.pcap.Reader(f)
        except Exception:
            return pts
        link_type = reader.datalink()
        for _, buf in reader:
            ip = _strip_link(buf, link_type)
            if ip is None:
                continue
            if not isinstance(ip, dpkt.ip.IP):
                continue
            if not isinstance(ip.data, dpkt.udp.UDP):
                continue
            payload = ip.data.data
            if len(payload) < 12:
                continue
            # RTP version=2, padding/ext/csrc bits all zero → byte0 == 0x80.
            if (payload[0] & 0xC0) != 0x80:
                continue
            pt = payload[1] & 0x7F
            # Skip RTCP packet types (200-204) which can also have v=2.
            if 200 <= pt <= 204:
                continue
            pts.add(pt)
    return pts


def _strip_link(buf: bytes, link_type: int):
    """Return the IP layer from a pcap frame, or None if not IPv4."""
    if link_type == dpkt.pcap.DLT_NULL:
        # 4-byte family header (AF_INET=2 little-endian on Linux)
        if len(buf) < 4:
            return None
        return dpkt.ip.IP(buf[4:])
    if link_type == dpkt.pcap.DLT_EN10MB:
        eth = dpkt.ethernet.Ethernet(buf)
        return eth.data
    if link_type == 113:  # DLT_LINUX_SLL — `tcpdump -i any`
        if len(buf) < 16:
            return None
        return dpkt.ip.IP(buf[16:])
    if link_type == 276:  # DLT_LINUX_SLL2 (newer kernels)
        if len(buf) < 20:
            return None
        return dpkt.ip.IP(buf[20:])
    return None
