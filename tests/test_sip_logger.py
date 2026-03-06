"""Unit tests for SipLogWriter / LogEntry."""

from __future__ import annotations

from collections import deque

from src.sip_logger import LogEntry, SipLogWriter


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
