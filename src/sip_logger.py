"""Custom PJSUA2 LogWriter — captures SIP messages into a bounded deque."""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass, field

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
_RE_FROM = re.compile(r"^From:[^\n]*<([^>]+)>", re.MULTILINE | re.IGNORECASE)
_RE_TO = re.compile(r"^To:[^\n]*<([^>]+)>", re.MULTILINE | re.IGNORECASE)
_RE_VIA = re.compile(
    r"^Via:\s*[^\n]*?\s+\S+:(\d+)", re.MULTILINE | re.IGNORECASE
)
# pjsua dump prefix: "[STATE] To: <URI>" — single-line, no full SIP body.
_RE_DUMP = re.compile(r"\[[A-Z_]+\]\s+To:\s*<([^>]+)>")


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
        md.from_uri = m.group(1)
    m = _RE_TO.search(msg)
    if m:
        md.to_uri = m.group(1)

    via_ports = tuple(int(p.group(1)) for p in _RE_VIA.finditer(msg))
    md.via_ports = via_ports

    # pjsua_app call dump: "[STATE] To: <URI>;tag=..."
    if md.sip_call_id is None and md.method is None:
        m = _RE_DUMP.search(msg)
        if m:
            md.dump_remote_uri = m.group(1)

    return md


_RE_ANGLE_URI = re.compile(r"<([^>]+)>")


def normalize_sip_uri(raw: str) -> str:
    """Reduce a SIP URI to its bare `sip:user@host` form.

    pjsua2's `CallInfo.remoteUri` returns RFC-3261 name-addr (with optional
    display name and angle brackets, plus `;tag=...` params), while pjsua's
    [DISCONNECTED] dump prints just the inner URI. Owner resolution
    requires both to look identical before set-membership.
    """
    if not raw:
        return ""
    m = _RE_ANGLE_URI.search(raw)
    bare = m.group(1) if m else raw
    return bare.split(";", 1)[0].strip()


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


def filter_entries_by_owner(
    entries: list[dict],
    phones: dict[str, PhoneMeta],
    target_phone: str | None = None,
    target_sip_call_id: str | None = None,
) -> tuple[list[dict], int]:
    """Filter log entries by structural ownership.

    target_phone — if set, keep only entries owned by that phone.
    target_sip_call_id — if set, additionally restrict to entries whose
        SIP Call-ID equals this value (used by `call_id=N` filter after
        resolving call_id → SipCall.callIdString).

    Returns (kept_entries, fallback_count). `fallback_count` is the number
    of entries that were kept only via substring username match (i.e. no
    structural signal matched, but the username appeared in msg). Callers
    should surface this as a warning so users know some attribution is
    heuristic.
    """
    if target_phone is None and target_sip_call_id is None:
        return list(entries), 0

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

        if target_phone is not None:
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
