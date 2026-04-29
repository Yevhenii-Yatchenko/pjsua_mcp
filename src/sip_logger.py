"""Custom PJSUA2 LogWriter — captures SIP messages into a bounded deque."""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import pjsua2 as pj


@dataclass
class LogEntry:
    level: int
    msg: str
    thread_name: str


@dataclass
class PhoneMeta:
    """Per-phone identity bundle used by ownership-based log filtering.

    `sip_call_ids` and `remote_uris` are populated from
    `CallManager.get_sip_call_id_index()` (the live tracker that grew when
    the phone made/received calls). `username` and `local_port` come from
    the phone's PhoneConfig + transport — they cover REGISTER and other
    out-of-dialog messages whose Call-ID never lands in the index.
    """

    phone_id: str
    username: str | None = None
    local_port: int | None = None
    sip_call_ids: set[str] = field(default_factory=set)
    remote_uris: set[str] = field(default_factory=set)


@dataclass
class SipMetadata:
    """Best-effort parse of a pjlib log entry into structured SIP fields.

    All fields default to None / empty — `get_sip_log` ownership resolution
    treats missing fields as "unknown" and falls through to the next signal
    (transport-port match → REGISTER username → substring fallback).
    """

    direction: str | None = None       # "TX" | "RX"
    method: str | None = None          # SIP method ("INVITE", "REGISTER", ...)
    cseq: int | None = None
    status_code: int | None = None     # for SIP responses
    sip_call_id: str | None = None     # SIP Call-ID header value
    from_uri: str | None = None
    to_uri: str | None = None
    via_ports: tuple[int, ...] = ()    # local ports seen in Via lines
    # pjsua_app's call dump emits `[<STATE>] To: <URI>;tag=...` (one line, no
    # SIP envelope, no Call-ID). When the prefix matches we extract that URI
    # so callers can match it against a phone's known remote URIs.
    dump_remote_uri: str | None = None


# Pre-compiled regexes — parse_sip_metadata is invoked once per filtered
# entry, so amortising the compile cost matters when get_sip_log walks
# thousands of entries.
# pjlib emits "<timestamp> <module>  TX/RX <n> bytes ..." on the first line,
# so TX/RX is not at start-of-line. Use \b instead of ^.
_RE_TXRX_REQUEST = re.compile(
    r"\b(TX|RX)\s+\d+\s+bytes\s+Request\s+msg\s+(\w+)/cseq=(\d+)",
)
_RE_TXRX_RESPONSE = re.compile(
    r"\b(TX|RX)\s+\d+\s+bytes\s+Response\s+msg\s+(\d{3})/(\w+)/cseq=(\d+)",
)
_RE_STATUS_LINE = re.compile(r"^SIP/2\.0\s+(\d{3})\s", re.MULTILINE)
_RE_CALL_ID = re.compile(r"^Call-ID:\s*(\S+)", re.MULTILINE | re.IGNORECASE)
_RE_CSEQ_HEADER = re.compile(
    r"^CSeq:\s*(\d+)\s+(\w+)", re.MULTILINE | re.IGNORECASE
)
# Capture the entire From/To value — display name + addr-spec + params.
# normalize_sip_uri then reduces it to the bare `sip:user@host` form.
# This handles both name-addr (`From: <sip:x>;tag=y`) and addr-spec
# (`From: sip:x;tag=y`) forms — pjsua emits the former, but other
# proxies sometimes route messages through the latter.
_RE_FROM = re.compile(r"^From:\s*([^\r\n]+)", re.MULTILINE | re.IGNORECASE)
_RE_TO = re.compile(r"^To:\s*([^\r\n]+)", re.MULTILINE | re.IGNORECASE)
_RE_VIA = re.compile(
    r"^Via:\s*[^\n]*?\s+\S+:(\d+)", re.MULTILINE | re.IGNORECASE
)
# pjsua dump prefix: "[STATE] To: <URI>" — single-line, no full SIP body.
_RE_DUMP = re.compile(r"\[[A-Z_]+\]\s+To:\s*<([^>]+)>")

_RE_ANGLE_URI = re.compile(r"<([^>]+)>")


def normalize_sip_uri(raw: str) -> str:
    """Reduce a SIP URI (name-addr or addr-spec) to its bare `sip:user@host`
    form. Strips display names, angle brackets, and `;tag=`/`;...=` params.

    Used by both `parse_sip_metadata` (to extract From/To URIs) and
    `_resolve_owner` (to compare a phone's tracker remote URI against
    pjsua's call-dump bare URI).
    """
    if not raw:
        return ""
    m = _RE_ANGLE_URI.search(raw)
    bare = m.group(1) if m else raw
    return bare.split(";", 1)[0].strip()


def parse_sip_metadata(msg: str) -> SipMetadata:
    """Extract structured fields from a pjlib log entry msg.

    Best-effort: returns a SipMetadata with `None` / empty fields if the
    entry isn't a SIP message. Never raises on malformed input.
    """
    md = SipMetadata()
    if not msg:
        return md

    # pjsua message envelope: "TX/RX N bytes Request msg METHOD/cseq=N ..."
    # or "TX/RX N bytes Response msg STATUS/METHOD/cseq=N ..."
    m_resp = _RE_TXRX_RESPONSE.search(msg)
    if m_resp:
        md.direction = m_resp.group(1)
        md.status_code = int(m_resp.group(2))
        md.method = m_resp.group(3).upper()
        md.cseq = int(m_resp.group(4))
    else:
        m_req = _RE_TXRX_REQUEST.search(msg)
        if m_req:
            md.direction = m_req.group(1)
            md.method = m_req.group(2).upper()
            md.cseq = int(m_req.group(3))

    # Status line "SIP/2.0 200 OK" — fallback if envelope didn't capture it
    if md.status_code is None:
        m = _RE_STATUS_LINE.search(msg)
        if m:
            md.status_code = int(m.group(1))

    # Call-ID header
    m = _RE_CALL_ID.search(msg)
    if m:
        md.sip_call_id = m.group(1)

    # Method from CSeq header — needed for SIP responses where the start
    # line is `SIP/2.0 200 OK`, not `INVITE ...`. CSeq's method matches the
    # request being acknowledged.
    if md.method is None or md.cseq is None:
        m = _RE_CSEQ_HEADER.search(msg)
        if m:
            if md.cseq is None:
                md.cseq = int(m.group(1))
            if md.method is None:
                md.method = m.group(2).upper()

    m = _RE_FROM.search(msg)
    if m:
        md.from_uri = normalize_sip_uri(m.group(1)) or None
    m = _RE_TO.search(msg)
    if m:
        md.to_uri = normalize_sip_uri(m.group(1)) or None

    via_ports = tuple(int(p.group(1)) for p in _RE_VIA.finditer(msg))
    md.via_ports = via_ports

    # pjsua_app call dump: "[STATE] To: <URI>;tag=..."
    if md.sip_call_id is None and md.method is None:
        m = _RE_DUMP.search(msg)
        if m:
            md.dump_remote_uri = m.group(1)

    return md


def _resolve_owner(
    md: SipMetadata,
    phones: dict[str, PhoneMeta],
) -> str | None:
    """Identify which phone owns the given log entry, or None if unknown.

    Returns the phone_id that owns the entry. Returns None when no signal
    matches — caller should treat as "unknown" and may fall back to
    substring matching.

    Note: returns a definite OWNER if any structural signal matches some
    phone — even if that phone is not the one being filtered for. The
    caller must compare to its target_phone to decide keep/drop.
    """
    # 1. SIP Call-ID — definitive
    if md.sip_call_id:
        for ph_id, meta in phones.items():
            if md.sip_call_id in meta.sip_call_ids:
                return ph_id

    # 2. pjsua dump — match the dump's `To: <URI>` against each phone's
    # remote URIs. The dump shows `To: <remote-party-uri>` for the phone
    # whose call was disconnected, which is *that* phone's remote URI.
    # Normalize both sides — pjsua2 stores remoteUri with display name
    # and tag params, dump emits the bare inner URI.
    if md.dump_remote_uri:
        target_uri = normalize_sip_uri(md.dump_remote_uri)
        for ph_id, meta in phones.items():
            if any(normalize_sip_uri(u) == target_uri for u in meta.remote_uris):
                return ph_id

    # 3. Via line — phone's local transport port appearing in any Via
    # header strongly attributes the message. (REGISTER, ACK, OPTIONS,
    # in-dialog requests with Call-ID we haven't tracked yet.)
    if md.via_ports:
        for ph_id, meta in phones.items():
            if meta.local_port and meta.local_port in md.via_ports:
                return ph_id

    # 4. REGISTER + From URI ↔ phone username. REGISTER's From URI carries
    # the registering account's user, so it's a clean signal.
    if md.method == "REGISTER" and md.from_uri:
        for ph_id, meta in phones.items():
            if meta.username and f"sip:{meta.username}@" in md.from_uri:
                return ph_id

    return None


# RFC 3551 static payload type table — the subset commonly seen in
# SIP-VoIP. Lets us synthesise codec entries for `m=audio 0 8 9` lines
# that don't carry explicit `a=rtpmap` (legal for static PTs).
_STATIC_PT_NAMES: dict[int, tuple[str, int]] = {
    0:  ("PCMU",   8000),
    3:  ("GSM",    8000),
    4:  ("G723",   8000),
    5:  ("DVI4",   8000),
    6:  ("DVI4",   16000),
    7:  ("LPC",    8000),
    8:  ("PCMA",   8000),
    9:  ("G722",   8000),
    10: ("L16",    44100),
    11: ("L16",    44100),
    12: ("QCELP",  8000),
    13: ("CN",     8000),
    14: ("MPA",    90000),
    15: ("G728",   8000),
    16: ("DVI4",   11025),
    17: ("DVI4",   22050),
    18: ("G729",   8000),
}


# Header names whose canonical form differs from naive title-case (RFC 3261
# casing conventions). Anything not in this map gets `.title()` treatment.
_HEADER_CANONICAL_CASE = {
    "call-id": "Call-ID",
    "cseq": "CSeq",
    "www-authenticate": "WWW-Authenticate",
    "mime-version": "MIME-Version",
    "p-asserted-identity": "P-Asserted-Identity",
    "p-preferred-identity": "P-Preferred-Identity",
}


def _canonical_header_name(name: str) -> str:
    low = name.strip().lower()
    if low in _HEADER_CANONICAL_CASE:
        return _HEADER_CANONICAL_CASE[low]
    # Title-case each hyphen-delimited token: "content-length" → "Content-Length"
    return "-".join(part.capitalize() for part in low.split("-"))


def parse_sip_headers(text: str | None) -> dict[str, str | list[str]]:
    """Extract SIP message headers as a dict.

    Accepts:
      - full SIP message text (start line + headers + blank line + body),
      - a headers-only block,
      - empty / None.

    Output:
      {Header-Name (canonical case): value}
      Multi-value headers (Via repeated through proxies, Route, etc.)
      collapse to a list with original arrival order preserved.

    The start line (`INVITE sip:... SIP/2.0` or `SIP/2.0 200 OK`) and
    the body (after the first blank line) are excluded from the dict.
    Lines without a `:` are silently skipped — pjlib log entries
    sometimes have stray text mixed in.
    """
    if not text:
        return {}

    lines = text.replace("\r\n", "\n").split("\n")

    # Trim everything from the first blank line onward (that's the body).
    end = len(lines)
    for i, line in enumerate(lines):
        if line == "":
            end = i
            break
    header_lines = lines[:end]

    # Skip the start line if present (request: METHOD URI SIP/2.0; response:
    # SIP/2.0 STATUS REASON). Both lack a colon-followed-by-space-and-value
    # pattern in the right position — easier to detect by content.
    if header_lines:
        first = header_lines[0].strip()
        if first.startswith("SIP/") or first.endswith("SIP/2.0"):
            header_lines = header_lines[1:]

    headers: dict[str, str | list[str]] = {}
    for raw_line in header_lines:
        line = raw_line.rstrip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        canon = _canonical_header_name(name)
        v = value.strip()
        if canon in headers:
            existing = headers[canon]
            if isinstance(existing, list):
                existing.append(v)
            else:
                headers[canon] = [existing, v]
        else:
            headers[canon] = v
    return headers


def parse_sdp_body(text: str | None) -> dict | None:
    """Parse a SIP message's SDP body into a structured dict.

    Output (per proposal-03):
      {
        "version": int,
        "origin": {"username": str, "ip": str},
        "media": [
          {"type", "port", "protocol", "payload_types", "codecs":
              [{"pt", "name", "clock_rate", "fmtp"?}],
           "direction", "rtcp_port"}
        ]
      }

    Returns None for empty / non-SDP input. Tolerates CRLF and bare-LF
    line endings, missing optional lines (b=, a=ssrc, a=rtcp), and static
    payload types without `a=rtpmap`.
    """
    if not text or "v=" not in text:
        return None

    lines = text.replace("\r\n", "\n").split("\n")
    if not any(line.startswith("v=") for line in lines):
        return None

    version: int | None = None
    origin: dict[str, str] | None = None
    media: list[dict] = []
    current_media: dict | None = None

    def _new_media(m_value: str) -> dict | None:
        # m=<type> <port> <proto> <pt> <pt> ...
        parts = m_value.split()
        if len(parts) < 4:
            return None
        try:
            port = int(parts[1])
            payload_types = [int(p) for p in parts[3:]]
        except ValueError:
            return None
        codecs = []
        for pt in payload_types:
            if pt in _STATIC_PT_NAMES:
                name, clock_rate = _STATIC_PT_NAMES[pt]
                codecs.append({"pt": pt, "name": name, "clock_rate": clock_rate})
            else:
                # Dynamic PT — entry will be filled by a later a=rtpmap.
                # Insert a placeholder so order is preserved.
                codecs.append({"pt": pt, "name": "", "clock_rate": 0})
        return {
            "type": parts[0],
            "port": port,
            "protocol": parts[2],
            "payload_types": payload_types,
            "codecs": codecs,
            "direction": "sendrecv",  # RFC 3264 default
            "rtcp_port": None,
        }

    def _apply_rtpmap(media_block: dict, value: str) -> None:
        # `<pt> <name>/<rate>[/<channels>]`
        try:
            pt_str, rest = value.split(" ", 1)
            pt = int(pt_str)
        except ValueError:
            return
        name_rate = rest.split("/")
        if not name_rate:
            return
        name = name_rate[0].strip()
        clock_rate = 0
        if len(name_rate) > 1:
            try:
                clock_rate = int(name_rate[1])
            except ValueError:
                pass
        for codec in media_block["codecs"]:
            if codec["pt"] == pt:
                codec["name"] = name
                codec["clock_rate"] = clock_rate
                return

    def _apply_fmtp(media_block: dict, value: str) -> None:
        try:
            pt_str, params = value.split(" ", 1)
            pt = int(pt_str)
        except ValueError:
            return
        for codec in media_block["codecs"]:
            if codec["pt"] == pt:
                codec["fmtp"] = params.strip()
                return

    for line in lines:
        line = line.rstrip()
        if not line or "=" not in line:
            continue
        kind, _, value = line.partition("=")
        if kind == "v":
            try:
                version = int(value)
            except ValueError:
                pass
        elif kind == "o":
            # `o=<username> <sess-id> <sess-version> IN IP4 <ip>`
            parts = value.split()
            if len(parts) >= 6:
                origin = {"username": parts[0], "ip": parts[5]}
        elif kind == "m":
            # New media section — close out previous if any.
            if current_media is not None:
                media.append(current_media)
            current_media = _new_media(value)
        elif kind == "a" and current_media is not None:
            attr_name, _, attr_val = value.partition(":")
            attr_name_low = attr_name.lower()
            if attr_name_low == "rtpmap":
                _apply_rtpmap(current_media, attr_val)
            elif attr_name_low == "fmtp":
                _apply_fmtp(current_media, attr_val)
            elif attr_name_low == "rtcp":
                # `a=rtcp:<port>[ <netaddr>]`
                rtcp_parts = attr_val.split()
                if rtcp_parts:
                    try:
                        current_media["rtcp_port"] = int(rtcp_parts[0])
                    except ValueError:
                        pass
            elif attr_name_low in ("sendrecv", "sendonly", "recvonly", "inactive"):
                current_media["direction"] = attr_name_low

    if current_media is not None:
        media.append(current_media)

    if version is None and not media:
        return None

    return {
        "version": version if version is not None else 0,
        "origin": origin,
        "media": media,
    }


_RE_TIMESTAMP = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d{3})")


def structurize_message(entry: dict) -> dict | None:
    """Compose a structured message dict from a raw pjlib log entry.

    Returns the proposal-03 message shape:
      {
        "ts", "direction", "method", "cseq", "call_id" (SIP Call-ID),
        "from", "to", "headers", "sdp",
        "status_code" (responses only),
      }

    Returns None for entries that aren't SIP messages (pjlib library log,
    pjsua call-dump summaries, REGISTER NAT-keepalive empty lines, etc.).
    Caller drops None values from the structured list.
    """
    if not entry:
        return None
    msg = entry.get("msg") or ""
    if not msg:
        return None

    md = parse_sip_metadata(msg)
    # Require an envelope — pjlib non-SIP entries lack TX/RX bytes line.
    # `direction` is the most reliable signal because both requests and
    # responses share it.
    if md.direction is None or (md.method is None and md.status_code is None):
        return None

    # Strip the pjlib envelope (first line ending with `:`) and the
    # `--end msg--` trailer that pjlib appends to multi-line dumps.
    sip_text = msg.split("\n", 1)[1] if "\n" in msg else ""
    sip_text = sip_text.rsplit("\n--end msg--", 1)[0]

    # Headers vs body — first blank line separates.
    sip_text_norm = sip_text.replace("\r\n", "\n")
    if "\n\n" in sip_text_norm:
        headers_part, _, body_part = sip_text_norm.partition("\n\n")
    else:
        headers_part, body_part = sip_text_norm, ""

    headers = parse_sip_headers(headers_part)

    sdp = None
    ct = headers.get("Content-Type", "")
    if isinstance(ct, str) and "application/sdp" in ct.lower():
        sdp = parse_sdp_body(body_part)

    ts_match = _RE_TIMESTAMP.search(msg)
    ts = ts_match.group(1) if ts_match else None

    structured: dict[str, Any] = {
        "ts": ts,
        "direction": md.direction,
        "method": md.method,
        "cseq": md.cseq,
        "call_id": md.sip_call_id,
        "from": md.from_uri,
        "to": md.to_uri,
        "headers": headers,
        "sdp": sdp,
    }
    if md.status_code is not None:
        structured["status_code"] = md.status_code
    return structured


def filter_entries_by_owner(
    entries: list[dict],
    phones: dict[str, PhoneMeta],
    target_phone: str | None = None,
    target_sip_call_id: str | None = None,
    method: str | None = None,
    direction: str | None = None,
    status_code: int | None = None,
    cseq: int | None = None,
) -> tuple[list[dict], int]:
    """Filter log entries by structural ownership and optional metadata.

    target_phone — if set, keep only entries owned by that phone.
    target_sip_call_id — if set, additionally restrict to entries whose
        SIP Call-ID equals this value (used by `call_id=N` filter after
        resolving call_id → SipCall.callIdString).
    method — SIP method (case-insensitive); excludes entries without a
        parseable method.
    direction — "TX" or "RX"; excludes entries without an envelope.
    status_code — exact status (200, 401, ...); excludes requests.
    cseq — CSeq number for tracking a single transaction.

    Returns (kept_entries, fallback_count). `fallback_count` counts entries
    kept only via substring username match (no structural signal — caller
    should surface as a warning).
    """
    no_filters = (
        target_phone is None
        and target_sip_call_id is None
        and method is None
        and direction is None
        and status_code is None
        and cseq is None
    )
    if no_filters:
        return list(entries), 0

    method_norm = method.upper() if method else None
    direction_norm = direction.upper() if direction else None

    target_meta = phones.get(target_phone) if target_phone else None
    target_username = target_meta.username if target_meta else None
    target_substring = (
        f"sip:{target_username}@" if target_username else None
    )

    kept: list[dict] = []
    fallback_count = 0
    for entry in entries:
        msg = entry.get("msg", "")
        md = parse_sip_metadata(msg)

        # Metadata filters — applied first so non-matching entries are
        # dropped without paying for ownership resolution. Each filter
        # excludes entries that lack the corresponding parsed field
        # (e.g. method=INVITE drops bare log lines with no method).
        if method_norm is not None and md.method != method_norm:
            continue
        if direction_norm is not None and md.direction != direction_norm:
            continue
        if status_code is not None and md.status_code != status_code:
            continue
        if cseq is not None and md.cseq != cseq:
            continue

        # Per-call filter — Call-ID match is required when target_sip_call_id
        # is set. Entries without a SIP Call-ID (or with a different one)
        # don't belong to this dialog.
        if target_sip_call_id is not None:
            if md.sip_call_id != target_sip_call_id:
                continue
            # If only call_id (no phone) — keep without further phone check.
            if target_phone is None:
                kept.append(entry)
                continue

        if target_phone is None:
            # No phone filter, but other metadata filters passed → keep.
            kept.append(entry)
            continue

        owner = _resolve_owner(md, phones)
        if owner == target_phone:
            kept.append(entry)
        elif owner is None:
            # Unknown owner — try substring fallback against the target
            # phone's username. Kept only if substring matches; counted
            # as fallback so the caller can emit a warning.
            if target_substring and target_substring in msg:
                kept.append(entry)
                fallback_count += 1
        # else: owned by a different phone — drop

    return kept, fallback_count


class SipLogWriter(pj.LogWriter):
    """Collects PJSUA2 log output into a thread-safe bounded deque.

    consoleLevel must be 0 in EpConfig so nothing hits stdout (the MCP channel).
    """

    def __init__(self, max_entries: int = 5000) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def write(self, entry: pj.LogEntry) -> None:
        le = LogEntry(
            level=entry.level,
            msg=entry.msg.rstrip("\n"),
            thread_name=entry.threadName,
        )
        with self._lock:
            self._entries.append(le)

    def get_entries(
        self,
        last_n: int | None = None,
        filter_text: str | None = None,
    ) -> list[dict]:
        with self._lock:
            entries = list(self._entries)
        if filter_text:
            entries = [e for e in entries if filter_text in e.msg]
        if last_n is not None:
            entries = entries[-last_n:]
        return [
            {"level": e.level, "msg": e.msg, "thread": e.thread_name}
            for e in entries
        ]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
