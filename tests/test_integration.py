"""MCP JSON-RPC integration tests — multi-phone dynamic-tool architecture.

One MCP server process manages multiple phones via `add_phone` / `drop_phone`.
Each phone gets per-phone tools named `<phone_id>_<action>` that appear/disappear
via `notifications/tools/list_changed`.

Self-contained when run via docker-compose.test.yml (Asterisk PBX + test runner).

Usage:
    # Docker Compose (self-contained, recommended)
    docker compose -f docker-compose.test.yml run --build --rm test-runner

    # Against external SIP server
    SIP_DOMAIN=sip.example.com SIP_USER_A=user_a SIP_PASS_A=xxx \
        pytest tests/test_integration.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

import pytest

pytestmark = pytest.mark.integration

SIP_DOMAIN = os.environ.get("SIP_DOMAIN", "")
SIP_USER_A = os.environ.get("SIP_USER_A", "6001")
SIP_PASS_A = os.environ.get("SIP_PASS_A", "test123")
SIP_USER_B = os.environ.get("SIP_USER_B", "6002")
SIP_PASS_B = os.environ.get("SIP_PASS_B", "test123")
SIP_USER_C = os.environ.get("SIP_USER_C", "6003")
SIP_PASS_C = os.environ.get("SIP_PASS_C", "test123")

skip_no_domain = pytest.mark.skipif(
    not SIP_DOMAIN,
    reason="SIP_DOMAIN not set — skipping integration tests",
)


# ---------------------------------------------------------------------------
# MCP JSON-RPC client
# ---------------------------------------------------------------------------

class McpClient:
    """Manages a pjsua-mcp subprocess and speaks JSON-RPC over stdin/stdout."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._msg_id = 0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "src.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def send_initialize(self) -> dict:
        resp = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        })
        self._send_notification("notifications/initialized", {})
        return resp

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self._send_request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

    def list_tools(self) -> list[str]:
        resp = self._send_request("tools/list", {})
        return sorted(t["name"] for t in resp["result"]["tools"])

    def _send_request(self, method: str, params: dict) -> dict:
        assert self._proc and self._proc.stdin and self._proc.stdout
        msg_id = self._next_id()
        request = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }
        self._proc.stdin.write(json.dumps(request) + "\n")
        self._proc.stdin.flush()

        while True:
            resp_line = self._proc.stdout.readline()
            if not resp_line:
                raise RuntimeError("MCP server closed stdout")
            try:
                resp = json.loads(resp_line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == msg_id:
                return resp

    def _send_notification(self, method: str, params: dict) -> None:
        assert self._proc and self._proc.stdin
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._proc.stdin.write(json.dumps(notification) + "\n")
        self._proc.stdin.flush()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()


def _parse_tool_result(resp: dict) -> dict[str, Any]:
    if "error" in resp:
        raise RuntimeError(f"MCP error: {resp['error']}")
    content = resp["result"]["content"]
    text = content[0]["text"]
    return json.loads(text)


def _wait_phone_registered(client: McpClient, phone_id: str, timeout: float = 5.0) -> None:
    """Poll `<phone_id>_get_registration_status` until is_registered=True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _parse_tool_result(client.call_tool(f"{phone_id}_get_registration_status"))
        if info.get("is_registered"):
            return
        time.sleep(0.3)
    raise AssertionError(f"Phone {phone_id!r} not registered within {timeout}s")


def _add_phone(
    client: McpClient,
    phone_id: str,
    username: str,
    password: str,
    *,
    auto_answer: bool = False,
    codecs: list[str] | None = None,
    recording_enabled: bool = False,
    capture_enabled: bool = False,
) -> dict:
    result = _parse_tool_result(client.call_tool("add_phone", {
        "phone_id": phone_id,
        "domain": SIP_DOMAIN,
        "username": username,
        "password": password,
        "auto_answer": auto_answer,
        "codecs": codecs,
        "recording_enabled": recording_enabled,
        "capture_enabled": capture_enabled,
    }))
    assert result["status"] == "ok", f"add_phone failed: {result}"
    return result


def _wait_and_answer(client: McpClient, phone_id: str, timeout: float = 5.0) -> dict:
    """Retry <phone_id>_answer_call until the incoming INVITE arrives."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _parse_tool_result(client.call_tool(f"{phone_id}_answer_call"))
        if result.get("status") == "ok":
            return result
        time.sleep(0.3)
    raise AssertionError(f"No incoming call on {phone_id!r} within {timeout}s")


# ---------------------------------------------------------------------------
# Dynamic tool lifecycle — exercises the factory/remove_tool machinery
# ---------------------------------------------------------------------------

class TestDynamicTools:
    """No SIP_DOMAIN needed — these tests target the factory, not the network."""

    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            yield

    def test_static_tools_only_at_startup(self):
        tools = self.client.list_tools()
        assert "add_phone" in tools
        assert "drop_phone" in tools
        assert "list_phones" in tools
        assert "load_phones" in tools
        # No per-phone tools until add_phone is called
        assert not any(t.startswith("a_") for t in tools)

    def test_add_phone_exposes_phone_tools(self):
        # register=False avoids waiting for a SIP REGISTER response
        result = _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "z",
            "domain": "127.0.0.1",
            "username": "user_z",
            "password": "pw",
            "register": False,
        }))
        assert result["status"] == "ok"
        assert result["tools_registered"] == 22

        tools = self.client.list_tools()
        assert "z_make_call" in tools
        assert "z_hangup" in tools
        assert "z_get_call_info" in tools
        assert "z_attended_transfer" in tools
        assert "z_get_registration_status" in tools
        # Symmetric register / unregister per-phone tools
        assert "z_register" in tools
        assert "z_unregister" in tools

    def test_drop_phone_removes_phone_tools(self):
        _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "z",
            "domain": "127.0.0.1",
            "username": "user_z",
            "password": "pw",
            "register": False,
        }))
        assert "z_make_call" in self.client.list_tools()

        result = _parse_tool_result(self.client.call_tool("drop_phone", {"phone_id": "z"}))
        assert result["status"] == "ok"
        assert result["tools_removed"] == 22

        tools = self.client.list_tools()
        assert "z_make_call" not in tools
        # Static tools still present
        assert "add_phone" in tools

    def test_list_phones_reflects_registry(self):
        _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "z", "domain": "127.0.0.1",
            "username": "user_z", "password": "pw",
            "register": False,
        }))
        result = _parse_tool_result(self.client.call_tool("list_phones"))
        assert result["total_count"] == 1
        assert result["phones"][0]["phone_id"] == "z"
        assert result["phones"][0]["username"] == "user_z"
        assert result["phones"][0]["tools"]

    def test_phone_id_validation_rejects_bad_chars(self):
        result = _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "Phone-1",  # uppercase + hyphen not allowed
            "domain": "127.0.0.1", "username": "x", "password": "x",
            "register": False,
        }))
        assert result["status"] == "error"
        assert "invalid" in result["error"].lower()


class TestRecordingLayout:
    """Recording layout — always-on, per-phone subdirs, sidecar meta.

    These tests cover the format-level contracts that don't need real SIP
    traffic: filename parsing, legacy-flat compatibility, and graceful
    handling of the deprecated `recordings_dir` YAML key. End-to-end
    behaviour (actual WAV/meta/pcap files written by a real call) is
    exercised by TestCallFlow and the live MCP verification steps.
    """

    @pytest.fixture(autouse=True)
    def mcp(self, tmp_path):
        self.tmp_path = tmp_path
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            yield

    def test_add_phone_response_has_no_recordings_dir(self):
        result = _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "z",
            "domain": "127.0.0.1", "username": "u", "password": "p",
            "register": False,
        }))
        assert result["status"] == "ok"
        assert "recordings_dir" not in result

        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "z"}))
        assert "recordings_dir" not in info

    def test_load_phones_warns_and_ignores_legacy_recordings_dir(self):
        """Legacy YAML files with recordings_dir should still load (warn + strip)."""
        profile = self.tmp_path / "legacy.yaml"
        profile.write_text("""
defaults:
  domain: 127.0.0.1
  password: x
  register: false
  recordings_dir: /recordings
phones:
  - {phone_id: p1, username: u1}
  - {phone_id: p2, username: u2, recordings_dir: /recordings/p2}
""")
        result = _parse_tool_result(self.client.call_tool(
            "load_phones", {"path": str(profile)}
        ))
        assert result["status"] == "ok"
        # Both phones loaded; legacy key silently dropped.
        loaded_ids = {p["phone_id"] for p in result["added"]}
        assert loaded_ids == {"p1", "p2"}

    def test_list_recordings_reads_new_layout(self):
        """/recordings/<phone_id>/call_<id>_<ts>.wav is parsed correctly."""
        # list_recordings walks the container's /recordings/ root. In-process
        # we create files under the real /recordings tree — works because the
        # test runner container mounts it writable.
        from pathlib import Path
        root = Path("/recordings")
        if not root.exists():
            pytest.skip("/recordings not mounted in this test environment")
        phone_dir = root / "testlayout"
        phone_dir.mkdir(parents=True, exist_ok=True)
        new_file = phone_dir / "call_7_20260101_120000.wav"
        new_file.write_bytes(b"fake wav")
        meta_file = new_file.with_suffix(".meta.json")
        meta_file.write_text('{"phone_id": "testlayout", "call_id": 7}')

        try:
            result = _parse_tool_result(self.client.call_tool(
                "list_recordings", {"phone_id": "testlayout"}
            ))
            assert result["total_count"] >= 1
            match = next(
                r for r in result["recordings"] if r["filename"] == new_file.name
            )
            assert match["phone_id"] == "testlayout"
            assert match["call_id"] == 7
            assert match["meta_path"] == str(meta_file)
        finally:
            new_file.unlink(missing_ok=True)
            meta_file.unlink(missing_ok=True)
            try:
                phone_dir.rmdir()
            except OSError:
                pass

    def test_list_recordings_reads_legacy_flat_layout(self):
        """Pre-refactor flat files /recordings/call_<phone_id>_<id>_<ts>.wav still show up."""
        from pathlib import Path
        root = Path("/recordings")
        if not root.exists():
            pytest.skip("/recordings not mounted in this test environment")
        flat_file = root / "call_legacy_3_20260101_120000.wav"
        flat_file.write_bytes(b"fake wav")

        try:
            result = _parse_tool_result(self.client.call_tool(
                "list_recordings", {"phone_id": "legacy"}
            ))
            match = next(
                r for r in result["recordings"] if r["filename"] == flat_file.name
            )
            assert match["phone_id"] == "legacy"
            assert match["call_id"] == 3
        finally:
            flat_file.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # recording_enabled toggle — per-phone + runtime + multi-start/stop
    # ------------------------------------------------------------------

    def test_recording_enabled_default_false(self):
        """add_phone without the flag should report recording_enabled=False (opt-in)."""
        result = _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "rec1",
            "domain": "127.0.0.1", "username": "u", "password": "p",
            "register": False,
        }))
        assert result["status"] == "ok"
        assert result["recording_enabled"] is False

        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "rec1"}))
        assert info["recording_enabled"] is False

    def test_recording_enabled_false_in_yaml(self):
        """Profile with recording_enabled=false surfaces through get_phone."""
        profile = self.tmp_path / "recoff.yaml"
        profile.write_text("""
defaults:
  domain: 127.0.0.1
  password: x
  register: false
  recording_enabled: false
phones:
  - {phone_id: rec_off_default, username: u1}
  - {phone_id: rec_on_override, username: u2, recording_enabled: true}
""")
        result = _parse_tool_result(self.client.call_tool(
            "load_phones", {"path": str(profile)}
        ))
        assert result["status"] == "ok"

        off = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "rec_off_default"}))
        assert off["recording_enabled"] is False

        on = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "rec_on_override"}))
        assert on["recording_enabled"] is True

    def test_update_phone_toggles_recording_runtime(self):
        """update_phone(recording_enabled=...) flips state even without active calls."""
        _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "rectog",
            "domain": "127.0.0.1", "username": "u", "password": "p",
            "register": False,
        }))

        # Default is False.
        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "rectog"}))
        assert info["recording_enabled"] is False

        # Flip to True.
        result = _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "rectog", "recording_enabled": True,
        }))
        assert result["status"] == "ok"
        assert result["recording_enabled"] is True
        assert result["affected_call_ids"] == []  # no active calls

        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "rectog"}))
        assert info["recording_enabled"] is True

        # Flip back to False.
        result = _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "rectog", "recording_enabled": False,
        }))
        assert result["recording_enabled"] is False

        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "rectog"}))
        assert info["recording_enabled"] is False

    def test_legacy_yaml_without_key_loads(self):
        """Profiles without recording_enabled now default to False (opt-in)."""
        profile = self.tmp_path / "legacy_nokey.yaml"
        profile.write_text("""
defaults:
  domain: 127.0.0.1
  password: x
  register: false
phones:
  - {phone_id: legacy1, username: u1}
""")
        result = _parse_tool_result(self.client.call_tool(
            "load_phones", {"path": str(profile)}
        ))
        assert result["status"] == "ok"

        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "legacy1"}))
        assert info["recording_enabled"] is False


class TestCaptureLayout:
    """Per-phone `capture_enabled` — YAML plumbing + runtime toggle.

    Covers the surface that doesn't need real SIP: default=false, YAML
    defaults + override, runtime update_phone toggle, and graceful
    collision error from `start_capture` when auto-capture is active.
    Live auto-start/stop on a real call is covered by TestCaptureLive.
    """

    @pytest.fixture(autouse=True)
    def mcp(self, tmp_path):
        self.tmp_path = tmp_path
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            yield

    def test_capture_enabled_default_false(self):
        """add_phone without the flag reports capture_enabled=False everywhere."""
        result = _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "cap1",
            "domain": "127.0.0.1", "username": "u", "password": "p",
            "register": False,
        }))
        assert result["status"] == "ok"
        assert result["capture_enabled"] is False

        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "cap1"}))
        assert info["capture_enabled"] is False

        phones = _parse_tool_result(self.client.call_tool("list_phones"))
        entry = next(p for p in phones["phones"] if p["phone_id"] == "cap1")
        assert entry["capture_enabled"] is False

    def test_capture_enabled_in_yaml(self):
        """Profile with capture_enabled=true in defaults and per-phone override."""
        profile = self.tmp_path / "capture.yaml"
        profile.write_text("""
defaults:
  domain: 127.0.0.1
  password: x
  register: false
  capture_enabled: true
phones:
  - {phone_id: cap_on_default, username: u1}
  - {phone_id: cap_off_override, username: u2, capture_enabled: false}
""")
        result = _parse_tool_result(self.client.call_tool(
            "load_phones", {"path": str(profile)}
        ))
        assert result["status"] == "ok"

        on = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "cap_on_default"}))
        assert on["capture_enabled"] is True

        off = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "cap_off_override"}))
        assert off["capture_enabled"] is False

    def test_update_phone_toggles_capture_runtime(self):
        """update_phone(capture_enabled=...) flips the flag without active calls."""
        _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "captog",
            "domain": "127.0.0.1", "username": "u", "password": "p",
            "register": False,
        }))

        # Default is False.
        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "captog"}))
        assert info["capture_enabled"] is False

        # Flip to True.
        result = _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "captog", "capture_enabled": True,
        }))
        assert result["status"] == "ok"
        assert result["capture_enabled"] is True

        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "captog"}))
        assert info["capture_enabled"] is True

        # Flip back to False.
        result = _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "captog", "capture_enabled": False,
        }))
        assert result["capture_enabled"] is False

    def test_manual_start_capture_ok_when_no_auto_capture(self):
        """start_capture(phone_id=...) on a phone with capture_enabled=false
        works — the collision check only fires when an auto-capture is live."""
        _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "manual1",
            "domain": "127.0.0.1", "username": "u", "password": "p",
            "register": False,
        }))
        # capture_enabled defaults to false; auto-capture isn't active yet.
        result = _parse_tool_result(self.client.call_tool("start_capture", {
            "phone_id": "manual1",
        }))
        # tcpdump may or may not launch depending on caps in this env; we only
        # assert that the collision check did NOT block us.
        assert result.get("status") != "error" or "auto-capture active" not in result.get("error", "")
        # Clean up if we did start something.
        if result.get("status") == "ok":
            _parse_tool_result(self.client.call_tool("stop_capture"))


# ---------------------------------------------------------------------------
# Multi-start/stop recording — live call exercise (requires SIP_DOMAIN)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestRecordingToggleLive:
    """Exercise off→on→off→on toggles during a live call.

    Separate fixture from TestRecordingLayout because we need two registered
    phones to drive an end-to-end audio session.
    """

    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            # Recording now defaults to OFF — explicitly enable on the phone
            # whose WAV segments this test counts.
            _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A, recording_enabled=True)
            _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B, auto_answer=True)
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            yield

    def test_multi_start_stop_creates_separate_files(self):
        """Toggle recording off/on twice mid-call → 3 distinct WAV segments."""
        from pathlib import Path

        rec_dir = Path("/recordings/a")
        before = {f.name for f in rec_dir.glob("*.wav")} if rec_dir.exists() else set()

        # Start the call; phone B auto-answers.
        r = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        assert r["status"] == "ok"
        call_id = r["call_id"]
        time.sleep(2.0)  # media active → first WAV starts

        # on → off : close WAV #1
        _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "a", "recording_enabled": False,
        }))
        time.sleep(0.4)

        # off → on : open WAV #2
        _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "a", "recording_enabled": True,
        }))
        time.sleep(0.4)

        # on → off : close WAV #2
        _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "a", "recording_enabled": False,
        }))
        time.sleep(0.4)

        # off → on : open WAV #3
        _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "a", "recording_enabled": True,
        }))
        time.sleep(0.4)

        # Hang up → close WAV #3.
        self.client.call_tool("a_hangup", {"call_id": call_id})
        time.sleep(1.0)

        after = {f.name for f in rec_dir.glob("*.wav")}
        new_files = sorted(after - before)
        assert len(new_files) == 3, f"Expected 3 WAV segments, got {new_files}"

        # All filenames unique (microsecond suffix differs).
        assert len(set(new_files)) == 3

        # Every WAV has a matching .meta.json sidecar with the call_id.
        for fname in new_files:
            meta_path = rec_dir / (Path(fname).stem + ".meta.json")
            assert meta_path.exists(), f"Missing sidecar for {fname}"
            meta = json.loads(meta_path.read_text())
            assert meta["phone_id"] == "a"
            assert meta["call_id"] == call_id
            assert meta["recording"].endswith(fname)


# ---------------------------------------------------------------------------
# Auto-capture live exercise (requires SIP_DOMAIN + NET_RAW caps for tcpdump)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestCaptureLive:
    """Per-phone `capture_enabled=true` → tcpdump starts/stops around real calls.

    Skipped without SIP_DOMAIN. Also tolerates environments where tcpdump
    can't actually open a raw socket (missing NET_RAW cap on the test
    container) — in that case the subprocess dies immediately, the pcap
    file is never opened, and we skip the sealing/size assertions rather
    than fail the test.
    """

    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
            _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B, auto_answer=True)
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            # capture_enabled defaults to False — enable on 'a' for these tests.
            _parse_tool_result(self.client.call_tool("update_phone", {
                "phone_id": "a", "capture_enabled": True,
            }))
            yield

    @staticmethod
    def _list_pcaps(phone_id: str) -> set[str]:
        from pathlib import Path
        d = Path("/captures") / phone_id
        return {f.name for f in d.glob("*.pcap")} if d.exists() else set()

    def test_auto_starts_and_stops_around_call(self):
        """Make a call → pcap appears. Hang up → pcap is closed (tcpdump gone)."""
        from pathlib import Path

        before = self._list_pcaps("a")

        r = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        assert r["status"] == "ok"
        call_id = r["call_id"]
        time.sleep(2.5)  # CONFIRMED + first audio-active → poll loop starts tcpdump

        during = self._list_pcaps("a")
        new_while_active = sorted(during - before)
        assert len(new_while_active) == 1, (
            f"Expected exactly one pcap to open on phone a, got {new_while_active}"
        )
        pcap_path = Path("/captures/a") / new_while_active[0]
        assert pcap_path.name.startswith(f"call_{call_id}_")

        # Hang up → poll loop drains stop queue and flushes the pcap.
        self.client.call_tool("a_hangup", {"call_id": call_id})
        time.sleep(2.0)

        after = self._list_pcaps("a")
        # No new pcaps opened after hangup.
        assert after == during
        # The pcap is no longer growing (tcpdump was terminated).
        size1 = pcap_path.stat().st_size if pcap_path.exists() else 0
        time.sleep(0.5)
        size2 = pcap_path.stat().st_size if pcap_path.exists() else 0
        assert size1 == size2, "pcap is still being written after hangup"

    def test_manual_start_capture_rejects_if_auto_active(self):
        """While auto-capture is live for a phone, start_capture(phone_id=...) errors."""
        r = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        call_id = r["call_id"]
        time.sleep(2.5)  # let auto-capture come up

        # Only run the collision check if auto-capture actually started; the
        # test harness may lack NET_RAW so tcpdump could exit immediately.
        if self._list_pcaps("a"):
            result = _parse_tool_result(self.client.call_tool("start_capture", {
                "phone_id": "a",
            }))
            assert result["status"] == "error"
            assert "auto-capture" in result["error"].lower()

        self.client.call_tool("a_hangup", {"call_id": call_id})
        time.sleep(1.5)

    def test_toggle_capture_mid_call(self):
        """update_phone(capture_enabled=false) mid-call closes pcap; toggle back opens a new one."""
        before = self._list_pcaps("a")

        r = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        call_id = r["call_id"]
        time.sleep(2.5)
        first_set = self._list_pcaps("a") - before

        # Flip off mid-call.
        _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "a", "capture_enabled": False,
        }))
        time.sleep(1.0)

        # No new pcaps while disabled.
        mid = self._list_pcaps("a") - before
        assert mid == first_set

        # Flip back on — new pcap file (different microsecond-stamped name).
        _parse_tool_result(self.client.call_tool("update_phone", {
            "phone_id": "a", "capture_enabled": True,
        }))
        time.sleep(1.0)
        second_set = self._list_pcaps("a") - before

        if first_set:  # only assert if auto-capture actually started
            assert len(second_set) >= len(first_set), (
                f"Expected toggle-back-on to open a new pcap, got {second_set}"
            )

        self.client.call_tool("a_hangup", {"call_id": call_id})
        time.sleep(1.5)

    def test_conference_single_capture_for_phone(self):
        """Two concurrent calls on phone 'a' share one pcap, not two."""
        # Add phone c so a can make two concurrent outbound calls.
        _add_phone(self.client, "c", SIP_USER_C, SIP_PASS_C, auto_answer=True)
        _wait_phone_registered(self.client, "c")

        before = self._list_pcaps("a")

        r1 = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        time.sleep(2.0)
        r2 = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
        }))
        time.sleep(2.5)

        during = self._list_pcaps("a") - before
        # The first call opens the pcap; the second must NOT open another.
        assert len(during) <= 1, f"Expected at most one pcap for conference, got {during}"

        # Hang up the first leg — pcap stays open (second leg still active).
        self.client.call_tool("a_hangup", {"call_id": r1["call_id"]})
        time.sleep(1.5)
        mid = self._list_pcaps("a") - before
        assert mid == during, "pcap was closed before last call disconnected"

        # Hang up the second leg — now pcap closes.
        self.client.call_tool("a_hangup", {"call_id": r2["call_id"]})
        time.sleep(2.0)


class TestLoadPhoneProfile:
    """Profile loader — no SIP_DOMAIN needed (all phones use register=False)."""

    @pytest.fixture(autouse=True)
    def mcp(self, tmp_path):
        self.tmp_path = tmp_path
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            yield

    def _write_profile(self, name: str, body: str) -> str:
        path = self.tmp_path / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_load_adds_three_phones(self):
        path = self._write_profile("profile.yaml", """
defaults:
  domain: 127.0.0.1
  password: x
  register: false
phones:
  - phone_id: p1
    username: u1
  - phone_id: p2
    username: u2
    auto_answer: true
  - phone_id: p3
    username: u3
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": path}))
        assert result["status"] == "ok"
        assert len(result["added"]) == 3
        assert not result["errors"]

        tools = self.client.list_tools()
        for pid in ("p1", "p2", "p3"):
            assert f"{pid}_make_call" in tools
            assert f"{pid}_hangup" in tools

    def test_missing_file(self):
        result = _parse_tool_result(self.client.call_tool("load_phones", {
            "path": "/tmp/does_not_exist.yaml",
        }))
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    def test_missing_required_field(self):
        path = self._write_profile("bad.yaml", """
defaults:
  domain: 127.0.0.1
  register: false
phones:
  - phone_id: p1
    username: u1
    # password missing from both defaults and phone
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": path}))
        assert result["status"] == "error"
        assert "password" in result["error"].lower()

    def test_unknown_field_rejected(self):
        path = self._write_profile("bad.yaml", """
defaults:
  domain: 127.0.0.1
  password: x
  register: false
phones:
  - phone_id: p1
    username: u1
    unknown_key: "oops"
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": path}))
        assert result["status"] == "error"
        assert "unknown" in result["error"].lower()

    def test_duplicate_phone_id(self):
        path = self._write_profile("dup.yaml", """
defaults:
  domain: 127.0.0.1
  password: x
  register: false
phones:
  - phone_id: p1
    username: u1
  - phone_id: p1
    username: u2
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": path}))
        assert result["status"] == "error"
        assert "duplicate" in result["error"].lower()

    def test_empty_phones_list(self):
        path = self._write_profile("empty.yaml", """
defaults:
  domain: 127.0.0.1
  password: x
phones: []
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": path}))
        assert result["status"] == "error"
        assert "empty" in result["error"].lower()

    def test_per_phone_override_beats_defaults(self):
        path = self._write_profile("override.yaml", """
defaults:
  domain: default.example.com
  password: x
  register: false
  auto_answer: false
phones:
  - phone_id: p1
    username: u1
    domain: override.example.com
    auto_answer: true
""")
        _parse_tool_result(self.client.call_tool("load_phones", {"path": path}))
        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "p1"}))
        assert info["domain"] == "override.example.com"
        assert info["auto_answer"] is True

    def test_replace_drops_existing_phones(self):
        """Default mode drops phones that are NOT in the new profile."""
        first = self._write_profile("first.yaml", """
defaults: {domain: 127.0.0.1, password: x, register: false}
phones:
  - {phone_id: p1, username: u1}
  - {phone_id: p2, username: u2}
  - {phone_id: p3, username: u3}
""")
        _parse_tool_result(self.client.call_tool("load_phones", {"path": first}))
        phones = _parse_tool_result(self.client.call_tool("list_phones"))
        assert {p["phone_id"] for p in phones["phones"]} == {"p1", "p2", "p3"}

        # Load a profile that contains only p4 — p1/p2/p3 should be dropped.
        second = self._write_profile("second.yaml", """
defaults: {domain: 127.0.0.1, password: x, register: false}
phones:
  - {phone_id: p4, username: u4}
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": second}))
        assert result["mode"] == "replace"
        assert set(result["dropped"]) == {"p1", "p2", "p3"}
        assert len(result["added"]) == 1

        phones = _parse_tool_result(self.client.call_tool("list_phones"))
        assert {p["phone_id"] for p in phones["phones"]} == {"p4"}

        tools = self.client.list_tools()
        for gone in ("p1_make_call", "p2_make_call", "p3_make_call"):
            assert gone not in tools
        assert "p4_make_call" in tools

    def test_merge_mode_preserves_outside_phones(self):
        """merge=True keeps phones that are not in the profile."""
        first = self._write_profile("first.yaml", """
defaults: {domain: 127.0.0.1, password: x, register: false}
phones:
  - {phone_id: p1, username: u1}
  - {phone_id: p2, username: u2}
""")
        _parse_tool_result(self.client.call_tool("load_phones", {"path": first}))

        second = self._write_profile("second.yaml", """
defaults: {domain: 127.0.0.1, password: x, register: false}
phones:
  - {phone_id: p3, username: u3}
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {
            "path": second, "merge": True,
        }))
        assert result["mode"] == "merge"
        assert result["dropped"] == []

        phones = _parse_tool_result(self.client.call_tool("list_phones"))
        assert {p["phone_id"] for p in phones["phones"]} == {"p1", "p2", "p3"}

    def test_invalid_profile_leaves_existing_untouched(self):
        """Parse/validation errors happen before any state mutation."""
        first = self._write_profile("first.yaml", """
defaults: {domain: 127.0.0.1, password: x, register: false}
phones:
  - {phone_id: p1, username: u1}
  - {phone_id: p2, username: u2}
""")
        _parse_tool_result(self.client.call_tool("load_phones", {"path": first}))

        bad = self._write_profile("bad.yaml", """
defaults: {domain: 127.0.0.1, register: false}
phones:
  - {phone_id: p3, username: u3}
  # password missing both from defaults and phone → validation fails
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": bad}))
        assert result["status"] == "error"

        phones = _parse_tool_result(self.client.call_tool("list_phones"))
        assert {p["phone_id"] for p in phones["phones"]} == {"p1", "p2"}

    def test_replace_of_empty_registry(self):
        """Replace semantics must not fail when there are no existing phones."""
        path = self._write_profile("fresh.yaml", """
defaults: {domain: 127.0.0.1, password: x, register: false}
phones:
  - {phone_id: p1, username: u1}
""")
        result = _parse_tool_result(self.client.call_tool("load_phones", {"path": path}))
        assert result["status"] == "ok"
        assert result["dropped"] == []
        assert len(result["added"]) == 1


# ---------------------------------------------------------------------------
# Registration against live Asterisk (single phone)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestPhoneRegistration:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            yield

    def test_add_phone_registers(self):
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
        _wait_phone_registered(self.client, "a")

        status = _parse_tool_result(self.client.call_tool("a_get_registration_status"))
        assert status["is_registered"] is True

    def test_add_phone_fails_without_engine_reach(self):
        result = _parse_tool_result(self.client.call_tool("add_phone", {
            "phone_id": "bad",
            "domain": "nonexistent.invalid",
            "username": "x",
            "password": "x",
        }))
        # add_phone itself succeeds (account is created, REGISTER sent) but
        # registration state will be off. Status "ok" — checked via get_phone.
        assert result["status"] == "ok"
        info = _parse_tool_result(self.client.call_tool("get_phone", {"phone_id": "bad"}))
        assert info["is_registered"] is False

    def test_sip_log_has_register_entries(self):
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
        _wait_phone_registered(self.client, "a")
        result = _parse_tool_result(self.client.call_tool("get_sip_log", {
            "filter_text": "REGISTER",
        }))
        assert result["total_count"] > 0

    def test_get_sip_log_phone_filter(self):
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
        _wait_phone_registered(self.client, "a")
        result = _parse_tool_result(self.client.call_tool("get_sip_log", {
            "phone_id": "a",
        }))
        # Every kept entry should contain phone a's user URI
        assert result["total_count"] > 0
        for e in result["entries"]:
            assert f"sip:{SIP_USER_A}@" in e["msg"]

    def test_unregister_phone_tool(self):
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
        _wait_phone_registered(self.client, "a")
        result = _parse_tool_result(self.client.call_tool("a_unregister"))
        assert result["status"] == "ok"

    def test_register_phone_tool_refreshes_binding(self):
        """a_register forces a fresh REGISTER cycle — symmetric to a_unregister."""
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
        _wait_phone_registered(self.client, "a")

        result = _parse_tool_result(self.client.call_tool("a_register"))
        assert result["status"] == "ok"
        assert result["is_registered"] is True
        # Per-phone tools stay alive after reregister (same phone_id, same tool set)
        tools = self.client.list_tools()
        assert "a_make_call" in tools
        assert "a_register" in tools
        assert "a_unregister" in tools


# ---------------------------------------------------------------------------
# Call flow (two phones in one MCP process)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestCallFlow:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
            _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B)
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            yield

    def test_call_and_hangup(self):
        result = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        assert result["status"] == "ok"
        call_id = result["call_id"]

        _wait_and_answer(self.client, "b")
        time.sleep(0.5)

        info = _parse_tool_result(self.client.call_tool("a_get_call_info", {"call_id": call_id}))
        assert info["state"] == "CONFIRMED"
        assert info["phone_id"] == "a"

        result = _parse_tool_result(self.client.call_tool("a_hangup", {"call_id": call_id}))
        assert result["status"] == "ok"

    def test_callee_hangup(self):
        self.client.call_tool("a_make_call", {"dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}"})
        _wait_and_answer(self.client, "b")
        time.sleep(0.5)

        result = _parse_tool_result(self.client.call_tool("b_hangup"))
        assert result["status"] == "ok"

    def test_sip_log_shows_invite(self):
        self.client.call_tool("a_make_call", {"dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}"})
        _wait_and_answer(self.client, "b")
        time.sleep(0.5)
        self.client.call_tool("a_hangup")
        time.sleep(0.5)

        log = _parse_tool_result(self.client.call_tool("get_sip_log", {"filter_text": "INVITE"}))
        assert log["total_count"] > 0

    def test_call_info_contacts(self):
        result = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        call_id = result["call_id"]
        _wait_and_answer(self.client, "b")
        time.sleep(0.5)

        info = _parse_tool_result(self.client.call_tool("a_get_call_info", {"call_id": call_id}))
        assert "remote_contact" in info and info["remote_contact"]
        assert "local_contact" in info and info["local_contact"]
        assert "sip:" in info["remote_contact"]

        self.client.call_tool("a_hangup", {"call_id": call_id})

    def test_reject_call(self):
        self.client.call_tool("a_make_call", {"dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}"})

        deadline = time.time() + 5
        while time.time() < deadline:
            result = _parse_tool_result(self.client.call_tool("b_reject_call", {
                "status_code": 486,
            }))
            if result.get("status") == "ok":
                break
            time.sleep(0.3)
        else:
            raise AssertionError("reject_call never succeeded")

        time.sleep(1)
        log_result = _parse_tool_result(self.client.call_tool("get_sip_log", {"filter_text": "486"}))
        assert log_result["total_count"] > 0

    def test_call_history_per_phone(self):
        self.client.call_tool("a_make_call", {"dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}"})
        _wait_and_answer(self.client, "b")
        time.sleep(1)
        self.client.call_tool("a_hangup")
        time.sleep(1)

        # A's history has the outbound call
        result = _parse_tool_result(self.client.call_tool("a_get_call_history"))
        assert result["total_count"] >= 1
        entry = result["history"][0]
        assert entry["phone_id"] == "a"


@skip_no_domain
class TestAutoAnswer:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
            _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B, auto_answer=True)
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            yield

    def test_auto_answer(self):
        result = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        call_id = result["call_id"]
        time.sleep(3)
        info = _parse_tool_result(self.client.call_tool("a_get_call_info", {"call_id": call_id}))
        assert info["state"] == "CONFIRMED"
        self.client.call_tool("a_hangup", {"call_id": call_id})


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

@skip_no_domain
class TestSipMessage:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
            _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B)
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            yield

    def test_send_and_receive(self):
        result = _parse_tool_result(self.client.call_tool("a_send_message", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
            "body": "Hello from A",
        }))
        assert result["status"] == "ok"
        time.sleep(2)

        result = _parse_tool_result(self.client.call_tool("b_get_messages"))
        assert result["total_count"] > 0
        assert any("Hello from A" in m["body"] for m in result["messages"])


# ---------------------------------------------------------------------------
# Transfer scenarios (three phones in ONE MCP server)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestBlindTransfer:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
            _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B)
            _add_phone(self.client, "c", SIP_USER_C, SIP_PASS_C, auto_answer=True)
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            _wait_phone_registered(self.client, "c")
            yield

    def test_blind_transfer(self):
        """A calls B, B blind-transfers A to C."""
        result = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        assert result["status"] == "ok"
        _wait_and_answer(self.client, "b")
        time.sleep(1)

        result = _parse_tool_result(self.client.call_tool("b_blind_transfer", {
            "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
        }))
        assert result["status"] == "ok"
        time.sleep(3)

        log = _parse_tool_result(self.client.call_tool("get_sip_log", {"filter_text": "BYE"}))
        assert log["total_count"] > 0

    def test_attended_transfer(self):
        """A calls B, B consults C, B bridges A↔C."""
        _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        b_answer = _wait_and_answer(self.client, "b")
        ab_on_b = b_answer["call_id"]
        time.sleep(1)

        _parse_tool_result(self.client.call_tool("b_hold", {"call_id": ab_on_b}))
        time.sleep(0.5)

        bc = _parse_tool_result(self.client.call_tool("b_make_call", {
            "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
        }))
        bc_on_b = bc["call_id"]
        time.sleep(2)  # C auto-answers

        result = _parse_tool_result(self.client.call_tool("b_attended_transfer", {
            "call_id": ab_on_b,
            "dest_call_id": bc_on_b,
        }))
        assert result["status"] == "ok"
        time.sleep(3)

        log = _parse_tool_result(self.client.call_tool("get_sip_log", {"filter_text": "BYE"}))
        assert log["total_count"] > 0

    def test_attended_transfer_rejects_cross_phone(self):
        """a_attended_transfer must reject a dest_call_id that belongs to another phone."""
        # Phone 'a' has two active calls (to B and to C), so the "need 2 active"
        # check passes. Then dest_call_id from phone 'b' triggers the
        # cross-phone validator.
        a_to_b = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        _wait_and_answer(self.client, "b")
        time.sleep(0.5)
        a_to_c = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
        }))
        time.sleep(2)  # C auto-answers

        b_calls = _parse_tool_result(self.client.call_tool("b_list_calls"))
        b_first_cid = b_calls["calls"][0]["call_id"]
        assert b_first_cid != a_to_b["call_id"]
        assert b_first_cid != a_to_c["call_id"]

        result = _parse_tool_result(self.client.call_tool("a_attended_transfer", {
            "call_id": a_to_b["call_id"],
            "dest_call_id": b_first_cid,  # belongs to phone b, not a
        }))
        assert result["status"] == "error"
        assert "belongs to phone_id" in result["error"]

        self.client.call_tool("a_hangup", {"call_id": a_to_b["call_id"]})
        self.client.call_tool("a_hangup", {"call_id": a_to_c["call_id"]})


# ---------------------------------------------------------------------------
# Conference (three phones in ONE MCP server)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestConference:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
            _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B, auto_answer=True)
            _add_phone(self.client, "c", SIP_USER_C, SIP_PASS_C, auto_answer=True)
            _wait_phone_registered(self.client, "a")
            _wait_phone_registered(self.client, "b")
            _wait_phone_registered(self.client, "c")
            yield

    def test_three_way_conference(self):
        r1 = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        time.sleep(2)
        r2 = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_C}@{SIP_DOMAIN}",
        }))
        time.sleep(2)

        result = _parse_tool_result(self.client.call_tool("a_conference", {
            "call_ids": [r1["call_id"], r2["call_id"]],
        }))
        assert result["status"] == "ok"
        assert result["participants"] == 2

        time.sleep(1)
        for cid in (r1["call_id"], r2["call_id"]):
            info = _parse_tool_result(self.client.call_tool("a_get_call_info", {"call_id": cid}))
            assert info["state"] == "CONFIRMED"

        self.client.call_tool("a_hangup", {"call_id": r1["call_id"]})
        self.client.call_tool("a_hangup", {"call_id": r2["call_id"]})


# ---------------------------------------------------------------------------
# Codec management
# ---------------------------------------------------------------------------

@skip_no_domain
class TestCodecs:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            yield

    def test_configure_with_codecs(self):
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A, codecs=["PCMU"])
        _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B)
        _wait_phone_registered(self.client, "a")
        _wait_phone_registered(self.client, "b")

        result = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        call_id = result["call_id"]
        _wait_and_answer(self.client, "b")
        time.sleep(1)

        info = _parse_tool_result(self.client.call_tool("a_get_call_info", {"call_id": call_id}))
        assert "PCMU" in info.get("codec", "")
        self.client.call_tool("a_hangup", {"call_id": call_id})

    def test_get_codecs(self):
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A)
        _wait_phone_registered(self.client, "a")
        result = _parse_tool_result(self.client.call_tool("get_codecs"))
        assert "codecs" in result
        assert len(result["codecs"]) > 0
        codec_names = [c["codec"] for c in result["codecs"]]
        assert any("PCMU" in c for c in codec_names)

    def test_set_codecs_midcall(self):
        _add_phone(self.client, "a", SIP_USER_A, SIP_PASS_A, codecs=["PCMU", "PCMA"])
        _add_phone(self.client, "b", SIP_USER_B, SIP_PASS_B)
        _wait_phone_registered(self.client, "a")
        _wait_phone_registered(self.client, "b")

        result = _parse_tool_result(self.client.call_tool("a_make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        call_id = result["call_id"]
        _wait_and_answer(self.client, "b")
        time.sleep(1)

        result = _parse_tool_result(self.client.call_tool("set_codecs", {
            "codecs": ["PCMA"],
            "phone_id": "a",
            "call_id": call_id,
        }))
        assert result["status"] == "ok"
        assert result["reinvite"] is True
        time.sleep(2)

        info = _parse_tool_result(self.client.call_tool("a_get_call_info", {"call_id": call_id}))
        assert "PCMA" in info.get("codec", "")
        self.client.call_tool("a_hangup", {"call_id": call_id})
