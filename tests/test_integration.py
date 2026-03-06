"""MCP JSON-RPC integration tests.

Self-contained when run via docker-compose.test.yml (Asterisk PBX + two
PJSUA MCP server subprocesses as caller/callee).

Can also target any SIP registrar by setting env vars manually.

Usage:
    # Docker Compose (self-contained, recommended)
    docker compose -f docker-compose.test.yml run --build --rm test-runner

    # Against external SIP server
    SIP_DOMAIN=192.168.1.202 SIP_USER_A=123007 SIP_PASS_A=xxx \
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
        """Send MCP initialize handshake."""
        resp = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        })
        self._send_notification("notifications/initialized", {})
        return resp

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call an MCP tool and return the raw JSON-RPC response."""
        return self._send_request("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

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
    """Extract parsed JSON from MCP tool response."""
    if "error" in resp:
        raise RuntimeError(f"MCP error: {resp['error']}")
    content = resp["result"]["content"]
    text = content[0]["text"]
    return json.loads(text)


def _wait_registered(client: McpClient, timeout: float = 5.0) -> None:
    """Poll get_registration_status until is_registered=True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = _parse_tool_result(client.call_tool("get_registration_status"))
        if info.get("is_registered"):
            return
        time.sleep(0.5)
    raise AssertionError(f"Registration not completed within {timeout}s")


def _configure_and_register(client: McpClient, user: str, password: str) -> None:
    """Helper: configure + register an MCP client, wait until registered."""
    result = _parse_tool_result(client.call_tool("configure", {
        "domain": SIP_DOMAIN,
        "transport": "udp",
        "username": user,
        "password": password,
    }))
    assert result["status"] == "configured"
    result = _parse_tool_result(client.call_tool("register"))
    assert result["status"] == "ok"
    _wait_registered(client)


# ---------------------------------------------------------------------------
# Registration tests (single account)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestRegistration:
    @pytest.fixture(autouse=True)
    def mcp(self):
        with McpClient() as client:
            client.send_initialize()
            self.client = client
            yield

    def test_configure_and_register(self):
        result = _parse_tool_result(self.client.call_tool("configure", {
            "domain": SIP_DOMAIN,
            "transport": "udp",
            "username": SIP_USER_A,
            "password": SIP_PASS_A,
        }))
        assert result["status"] == "configured"

        result = _parse_tool_result(self.client.call_tool("register"))
        assert result["status"] == "ok"
        # Registration may still be in-flight; poll until complete.
        _wait_registered(self.client)

    def test_get_registration_status(self):
        _configure_and_register(self.client, SIP_USER_A, SIP_PASS_A)

        result = _parse_tool_result(
            self.client.call_tool("get_registration_status")
        )
        assert result["is_registered"] is True

    def test_register_without_configure(self):
        result = _parse_tool_result(self.client.call_tool("register"))
        assert result["status"] == "error"

    def test_get_sip_log_has_entries(self):
        _configure_and_register(self.client, SIP_USER_A, SIP_PASS_A)

        result = _parse_tool_result(self.client.call_tool("get_sip_log"))
        assert result["total_count"] > 0

    def test_get_sip_log_filter(self):
        _configure_and_register(self.client, SIP_USER_A, SIP_PASS_A)

        all_result = _parse_tool_result(self.client.call_tool("get_sip_log"))
        filtered = _parse_tool_result(self.client.call_tool("get_sip_log", {
            "filter_text": "REGISTER",
        }))
        assert filtered["total_count"] <= all_result["total_count"]
        assert filtered["total_count"] > 0

    def test_unregister(self):
        _configure_and_register(self.client, SIP_USER_A, SIP_PASS_A)

        result = _parse_tool_result(self.client.call_tool("unregister"))
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# Call-flow tests (two accounts)
# ---------------------------------------------------------------------------

@skip_no_domain
class TestCallFlow:
    @pytest.fixture(autouse=True)
    def mcp_pair(self):
        with McpClient() as caller, McpClient() as callee:
            caller.send_initialize()
            callee.send_initialize()
            self.caller = caller
            self.callee = callee
            yield

    def _register_both(self) -> None:
        _configure_and_register(self.caller, SIP_USER_A, SIP_PASS_A)
        _configure_and_register(self.callee, SIP_USER_B, SIP_PASS_B)

    @staticmethod
    def _wait_and_answer(client: McpClient, timeout: float = 5.0) -> dict:
        """Retry answer_call until the incoming INVITE arrives."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = _parse_tool_result(client.call_tool("answer_call"))
            if result.get("status") == "ok":
                return result
            time.sleep(0.5)
        raise AssertionError(f"No incoming call within {timeout}s")

    def test_call_and_hangup(self):
        self._register_both()

        # Caller dials callee
        result = _parse_tool_result(self.caller.call_tool("make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        }))
        assert result["status"] == "ok"
        call_id = result["call_id"]

        # Callee answers
        self._wait_and_answer(self.callee)
        time.sleep(0.5)

        # Verify caller side shows CONFIRMED
        caller_info = _parse_tool_result(
            self.caller.call_tool("get_call_info", {"call_id": call_id})
        )
        assert caller_info["state"] == "CONFIRMED"

        # Hangup from caller
        result = _parse_tool_result(
            self.caller.call_tool("hangup", {"call_id": call_id})
        )
        assert result["status"] == "ok"

    def test_callee_hangup(self):
        self._register_both()

        self.caller.call_tool("make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        })
        self._wait_and_answer(self.callee)
        time.sleep(0.5)

        # Hangup from callee side
        result = _parse_tool_result(self.callee.call_tool("hangup"))
        assert result["status"] == "ok"

    def test_sip_log_shows_invite(self):
        self._register_both()

        self.caller.call_tool("make_call", {
            "dest_uri": f"sip:{SIP_USER_B}@{SIP_DOMAIN}",
        })
        self._wait_and_answer(self.callee)
        time.sleep(0.5)
        self.caller.call_tool("hangup")
        time.sleep(0.5)

        log = _parse_tool_result(self.caller.call_tool("get_sip_log", {
            "filter_text": "INVITE",
        }))
        assert log["total_count"] > 0
