"""Filter audio codecs in an SDP string.

Mutates only `m=audio` blocks: keeps payload types whose `a=rtpmap`
encoding name is in the allow-list (case-insensitive), drops the rest,
and strips dangling `a=rtpmap` / `a=fmtp` lines for removed PTs. Every
other section (m=video, m=image, c=, t=, session-level a=) is copied
verbatim.

Designed for use inside `Call.onCallSdpCreated` — the C++ bridge in
pjsua2 (`Endpoint::on_call_sdp_created`) re-parses `prm.sdp.wholeSdp`
into a fresh `pjmedia_sdp_session` after we return, so this rewriter's
output must be valid SDP. Empty allow-list and empty input return the
original string untouched.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

log = logging.getLogger(__name__)

_RTPMAP_RE = re.compile(r"^a=rtpmap:(\d+)\s+([^/\s]+)(?:/.*)?$")
_FMTP_RE = re.compile(r"^a=fmtp:(\d+)\b")
_M_LINE_RE = re.compile(
    r"^m=(?P<media>\w+)\s+(?P<port>\d+)\s+(?P<proto>[\w/]+)\s+(?P<fmts>.+)$"
)

# RFC 3551 static payload-type table (audio only). PTs without an explicit
# `a=rtpmap` line in SDP fall back to these names.
_STATIC_PT = {
    "0": "pcmu",
    "3": "gsm",
    "8": "pcma",
    "9": "g722",
    "15": "g728",
    "18": "g729",
}


def filter_audio_codecs(
    sdp: str,
    allowed: Iterable[str],
    *,
    preserve_dtmf: bool = False,
) -> str:
    """Return an SDP string with `m=audio` codecs filtered to `allowed`.

    `allowed` is matched case-insensitively against the encoding name in
    `a=rtpmap` (e.g. `"PCMA"`, `"telephone-event"`). PTs in the original
    `m=audio` format list with no matching `a=rtpmap` (static PTs like
    PCMU=0, PCMA=8) are matched against the static codec table.

    `preserve_dtmf=True` always keeps `telephone-event` even if not in
    the allow-list — useful when the caller explicitly wants DTMF pass-
    through but the wishlist is just media codecs.

    Empty `allowed` (or all-falsy) is a no-op — original returned. We
    do this rather than producing a codec-less m=audio (which is
    invalid SDP and would cause pjsua to log a parse failure and revert
    to the unfiltered original anyway).
    """
    allowed_set = {a.lower() for a in allowed if a}
    if not allowed_set:
        return sdp
    if preserve_dtmf:
        allowed_set.add("telephone-event")
    if not sdp:
        return sdp

    line_sep = "\r\n" if "\r\n" in sdp else "\n"
    lines = sdp.split(line_sep)

    blocks = _scan_audio_blocks(lines, allowed_set)

    # Bail-out: if any m=audio block ends up with zero kept PTs, return
    # the input untouched. Emitting an empty audio format list is invalid
    # SDP and pjsua's pjmedia_sdp_parse would silently revert anyway.
    for blk in blocks:
        if not blk["keep"]:
            log.warning(
                "filter_audio_codecs: m=audio at line %d would be empty "
                "after filter (allowed=%s, fmts=%s) — returning unchanged",
                blk["start"], sorted(allowed_set), blk["fmts"],
            )
            return sdp

    return _emit(lines, blocks, line_sep)


def _scan_audio_blocks(lines: list[str], allowed_set: set[str]) -> list[dict]:
    """First pass: identify each m=audio block and its kept PTs."""
    blocks: list[dict] = []
    cur: dict | None = None
    for i, line in enumerate(lines):
        m = _M_LINE_RE.match(line)
        if m:
            if cur is not None:
                cur["end"] = i
                blocks.append(cur)
                cur = None
            if m.group("media") == "audio":
                cur = {
                    "start": i,
                    "end": len(lines),
                    "fmts": m.group("fmts").split(),
                    "keep": set(),
                }
        elif cur is not None:
            rm = _RTPMAP_RE.match(line)
            if rm:
                pt, name = rm.group(1), rm.group(2).lower()
                if name in allowed_set:
                    cur["keep"].add(pt)
    if cur is not None:
        blocks.append(cur)

    # Static PT fallback for PTs without rtpmap.
    for blk in blocks:
        for pt in blk["fmts"]:
            if pt in blk["keep"]:
                continue
            name = _STATIC_PT.get(pt)
            if name and name in allowed_set:
                blk["keep"].add(pt)

    return blocks


def _emit(lines: list[str], blocks: list[dict], line_sep: str) -> str:
    """Second pass: emit lines, rewriting m=audio and dropping dangling
    a=rtpmap / a=fmtp for removed PTs."""
    out: list[str] = []
    block_iter = iter(blocks)
    cur_blk = next(block_iter, None)
    for i, line in enumerate(lines):
        while cur_blk and i >= cur_blk["end"]:
            cur_blk = next(block_iter, None)

        m = _M_LINE_RE.match(line)
        if m and cur_blk and i == cur_blk["start"]:
            ordered = _order_fmts(cur_blk["fmts"], cur_blk["keep"])
            out.append(
                f"m={m.group('media')} {m.group('port')} "
                f"{m.group('proto')} {' '.join(ordered)}"
            )
            continue

        if cur_blk and cur_blk["start"] < i < cur_blk["end"]:
            rm = _RTPMAP_RE.match(line)
            if rm and rm.group(1) not in cur_blk["keep"]:
                continue
            fm = _FMTP_RE.match(line)
            if fm and fm.group(1) not in cur_blk["keep"]:
                continue

        out.append(line)

    return line_sep.join(out)


def _order_fmts(fmts: list[str], keep: set[str]) -> list[str]:
    """Preserve original m= order for kept PTs (we don't try to enforce
    a different priority — pjsua already orders fmts in its INVITE
    based on its endpoint codec priorities, which we pin to the superset
    at startup)."""
    return [f for f in fmts if f in keep]
