"""Custom PJSUA2 LogWriter — captures SIP messages into a bounded deque."""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

import pjsua2 as pj


@dataclass
class LogEntry:
    level: int
    msg: str
    thread_name: str


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
