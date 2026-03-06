"""PJSUA MCP Server — FastMCP entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any

# --- Protect MCP stdio channel from pjlib C-level console output ---
# pjlib writes directly to C fd 1 (stdout) regardless of consoleLevel during
# early init. We redirect fd 1 → stderr and give Python sys.stdout its own fd.
_mcp_stdout_fd = os.dup(1)          # save original stdout fd for MCP
os.dup2(2, 1)                       # C stdout (fd 1) → stderr
sys.stdout = os.fdopen(_mcp_stdout_fd, "w", buffering=1)  # line-buffered

from mcp.server.fastmcp import FastMCP

from .sip_engine import SipEngine
from .account_manager import AccountManager
from .call_manager import CallManager
from .pcap_manager import PcapManager

# All Python logging goes to stderr — stdout is the MCP channel
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("pjsua_mcp")

engine: SipEngine | None = None
account_mgr: AccountManager | None = None
call_mgr: CallManager | None = None
pcap_mgr: PcapManager | None = None
_poll_task: asyncio.Task | None = None


async def _poll_pjsip_events(eng: SipEngine) -> None:
    """Background task: poll pjsip event loop from asyncio thread.

    Calls libHandleEvents(10) directly (not via executor) because SWIG director
    callbacks for LogWriter don't work from executor threads (GIL issue).
    The short timeout keeps the event loop responsive.
    """
    while True:
        try:
            eng.handle_events(10)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error in pjsip event poll")
        await asyncio.sleep(0.02)  # ~50 polls/sec, yields to MCP handlers


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage PJSUA2 lifecycle alongside MCP server."""
    global engine, account_mgr, call_mgr, pcap_mgr, _poll_task
    log.info("PJSUA MCP server starting")
    engine = SipEngine()
    account_mgr = AccountManager(engine)
    call_mgr = CallManager(engine, account_mgr)
    pcap_mgr = PcapManager()
    yield
    # Shutdown
    log.info("PJSUA MCP server shutting down")
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    await pcap_mgr.cleanup()
    call_mgr.hangup_all()
    account_mgr.unregister_all()
    engine.shutdown()
    log.info("PJSUA MCP server stopped")


mcp = FastMCP("pjsua", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Tool: configure
# ---------------------------------------------------------------------------
@mcp.tool()
async def configure(
    domain: str,
    transport: str = "udp",
    username: str | None = None,
    password: str | None = None,
    realm: str | None = None,
    srtp: bool = False,
    local_port: int = 0,
) -> dict[str, Any]:
    """Initialize the SIP engine: create endpoint, set transport, and prepare for registration.

    Args:
        domain: SIP domain / registrar address (e.g. "sip.example.com")
        transport: Transport protocol — "udp", "tcp", or "tls" (default "udp")
        username: SIP username for authentication
        password: SIP password for authentication
        realm: SIP realm (defaults to "*" if not specified)
        srtp: Enable SRTP media encryption (default False)
        local_port: Local port to bind (0 = auto)
    """
    global _poll_task
    assert engine is not None and account_mgr is not None

    try:
        transport_id = engine.initialize(
            transport=transport,
            local_port=local_port,
        )
        account_mgr.configure(
            domain=domain,
            username=username,
            password=password,
            realm=realm,
            srtp=srtp,
        )
        # Start polling pjsip events after initialization
        if _poll_task is None or _poll_task.done():
            _poll_task = asyncio.create_task(_poll_pjsip_events(engine))

        return {
            "status": "configured",
            "transport": transport,
            "transport_id": transport_id,
            "domain": domain,
        }
    except Exception as e:
        log.exception("configure failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: register / unregister / get_registration_status
# ---------------------------------------------------------------------------
@mcp.tool()
async def register() -> dict[str, Any]:
    """Register the configured SIP account with the registrar."""
    assert account_mgr is not None
    try:
        account_mgr.register()
        # Give registration a moment to process
        await asyncio.sleep(1)
        info = account_mgr.get_registration_info()
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("register failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def unregister() -> dict[str, Any]:
    """Unregister the current SIP account."""
    assert account_mgr is not None
    try:
        account_mgr.unregister()
        await asyncio.sleep(0.5)
        return {"status": "ok"}
    except Exception as e:
        log.exception("unregister failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def get_registration_status() -> dict[str, Any]:
    """Get current SIP registration status."""
    assert account_mgr is not None
    try:
        return account_mgr.get_registration_info()
    except Exception as e:
        log.exception("get_registration_status failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: make_call / answer_call / hangup / get_call_info
# ---------------------------------------------------------------------------
@mcp.tool()
async def make_call(
    dest_uri: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Place an outbound SIP call.

    Args:
        dest_uri: Destination SIP URI (e.g. "sip:1001@sip.example.com")
        headers: Optional extra SIP headers as key-value pairs
    """
    assert call_mgr is not None
    try:
        info = call_mgr.make_call(dest_uri, headers=headers)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("make_call failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def answer_call(
    call_id: int | None = None,
    status_code: int = 200,
) -> dict[str, Any]:
    """Answer an incoming SIP call.

    Args:
        call_id: Call ID to answer (default: first incoming call)
        status_code: SIP response code (default 200 OK)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.answer_call(call_id=call_id, status_code=status_code)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("answer_call failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def hangup(call_id: int | None = None) -> dict[str, Any]:
    """Hang up a SIP call.

    Args:
        call_id: Call ID to hang up (default: current active call)
    """
    assert call_mgr is not None
    try:
        call_mgr.hangup(call_id=call_id)
        return {"status": "ok"}
    except Exception as e:
        log.exception("hangup failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def get_call_info(call_id: int | None = None) -> dict[str, Any]:
    """Get information about a SIP call.

    Args:
        call_id: Call ID to query (default: current active call)
    """
    assert call_mgr is not None
    try:
        return call_mgr.get_call_info(call_id=call_id)
    except Exception as e:
        log.exception("get_call_info failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def send_dtmf(call_id: int, digits: str) -> dict[str, Any]:
    """Send DTMF digits on an active call.

    Args:
        call_id: Call ID
        digits: DTMF digits to send (e.g. "1234#")
    """
    assert call_mgr is not None
    try:
        call_mgr.send_dtmf(call_id, digits)
        return {"status": "ok", "digits": digits}
    except Exception as e:
        log.exception("send_dtmf failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def hold(call_id: int) -> dict[str, Any]:
    """Put a call on hold.

    Args:
        call_id: Call ID to hold
    """
    assert call_mgr is not None
    try:
        call_mgr.hold(call_id)
        return {"status": "ok"}
    except Exception as e:
        log.exception("hold failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def unhold(call_id: int) -> dict[str, Any]:
    """Resume a held call.

    Args:
        call_id: Call ID to resume
    """
    assert call_mgr is not None
    try:
        call_mgr.unhold(call_id)
        return {"status": "ok"}
    except Exception as e:
        log.exception("unhold failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: SIP log & packet capture
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_sip_log(
    last_n: int | None = None,
    filter_text: str | None = None,
) -> dict[str, Any]:
    """Retrieve SIP log entries from the PJSUA2 logger.

    Args:
        last_n: Return only the last N entries (default: all)
        filter_text: Filter entries containing this text
    """
    assert engine is not None
    try:
        entries = engine.get_log_entries(last_n=last_n, filter_text=filter_text)
        return {
            "entries": entries,
            "total_count": len(entries),
        }
    except Exception as e:
        log.exception("get_sip_log failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def start_capture(
    interface: str = "any",
    port: int | None = None,
) -> dict[str, Any]:
    """Start a packet capture using tcpdump.

    Args:
        interface: Network interface to capture on (default "any")
        port: Filter by port number (default: capture all SIP ports)
    """
    assert pcap_mgr is not None
    try:
        info = await pcap_mgr.start(interface=interface, port=port)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("start_capture failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def stop_capture() -> dict[str, Any]:
    """Stop the running packet capture."""
    assert pcap_mgr is not None
    try:
        info = await pcap_mgr.stop()
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("stop_capture failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def get_pcap(filename: str | None = None) -> dict[str, Any]:
    """Get information about a captured pcap file.

    Args:
        filename: Specific pcap filename (default: most recent capture)
    """
    assert pcap_mgr is not None
    try:
        info = pcap_mgr.get_pcap_info(filename=filename)
        return info
    except Exception as e:
        log.exception("get_pcap failed")
        return {"status": "error", "error": str(e)}


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
