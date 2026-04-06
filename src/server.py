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
            # Process deferred actions after pjsip events
            if call_mgr:
                call_mgr.process_auto_answers()
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
    auto_answer: bool = False,
    codecs: list[str] | None = None,
) -> dict[str, Any]:
    """Configure (or reconfigure) the SIP user agent.

    On first call: initializes the SIP engine, creates transport, stores credentials.
    On subsequent calls: unregisters the current account and updates credentials
    without restarting the engine — useful for changing domain, username, or password.

    Args:
        domain: SIP domain / registrar address (e.g. "sip.example.com")
        transport: Transport protocol — "udp", "tcp", or "tls" (default "udp")
        username: SIP username for authentication
        password: SIP password for authentication
        realm: SIP realm (defaults to "*" if not specified)
        srtp: Enable SRTP media encryption (default False)
        local_port: Local port to bind (0 = auto)
        auto_answer: Automatically answer incoming calls with 200 OK (default False)
        codecs: List of codecs in priority order, e.g. ["PCMU", "G722"]. Others disabled. Default: all enabled.
    """
    global _poll_task
    assert engine is not None and account_mgr is not None

    try:
        transport_id: int | None = None

        if engine.initialized:
            # Reconfiguration: tear down the old account, keep the engine
            log.info("Reconfiguring — unregistering old account")
            call_mgr.hangup_all()
            account_mgr.unregister_all()
            await asyncio.sleep(0.5)
        else:
            # First-time initialization
            transport_id = engine.initialize(
                transport=transport,
                local_port=local_port,
            )
            if _poll_task is None or _poll_task.done():
                _poll_task = asyncio.create_task(_poll_pjsip_events(engine))

        account_mgr.configure(
            domain=domain,
            username=username,
            password=password,
            realm=realm,
            srtp=srtp,
            auto_answer=auto_answer,
        )

        # Set codec priorities if specified
        enabled_codecs = None
        if codecs and engine.initialized:
            enabled_codecs = engine.set_codecs(codecs)

        return {
            "status": "configured",
            "transport": transport,
            "transport_id": transport_id,
            "domain": domain,
            "codecs": enabled_codecs,
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
    assert account_mgr is not None and call_mgr is not None
    try:
        account_mgr.register()
        # Wire up incoming call handler now that account exists
        call_mgr._ensure_incoming_handler()
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
async def reject_call(call_id: int | None = None, status_code: int = 486) -> dict[str, Any]:
    """Reject an incoming call with a SIP error response.

    Args:
        call_id: Call ID to reject (default: first incoming call)
        status_code: SIP response code — 486 (Busy), 603 (Decline), 480 (Unavailable)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.reject_call(call_id=call_id, status_code=status_code)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("reject_call failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def blind_transfer(dest_uri: str, call_id: int | None = None) -> dict[str, Any]:
    """Blind transfer: redirect an active call to another SIP URI.

    Sends SIP REFER, causing the remote party to send a new INVITE
    to dest_uri. Our side of the call is then disconnected.

    Args:
        dest_uri: Transfer destination (e.g. "sip:6003@asterisk")
        call_id: Call ID to transfer (default: current active call)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.blind_transfer(dest_uri, call_id=call_id)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("blind_transfer failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def attended_transfer(
    call_id: int | None = None,
    dest_call_id: int | None = None,
) -> dict[str, Any]:
    """Attended transfer: bridge two active calls and disconnect ourselves.

    Must have two active calls (e.g. original call on hold + consultation call).
    Sends REFER with Replaces to connect the two remote parties directly.

    Args:
        call_id: Source call to transfer (default: auto-select first active)
        dest_call_id: Destination call to replace (default: auto-select second active)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.attended_transfer(call_id=call_id, dest_call_id=dest_call_id)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("attended_transfer failed")
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
# Tool: audio playback
# ---------------------------------------------------------------------------
@mcp.tool()
async def play_audio(
    file_path: str,
    call_id: int | None = None,
    loop: bool = False,
) -> dict[str, Any]:
    """Play a WAV file into an active call (replaces current audio/MOH).

    By default, all calls play Music-on-Hold automatically. Use this tool to
    play a specific file (e.g. TTS output, IVR prompts, pre-recorded messages).

    Args:
        file_path: Path to WAV file inside the container (e.g. "/recordings/call_1_20260306.wav")
        call_id: Call ID (default: current active call)
        loop: Loop the file (default False — plays once, then resumes MOH)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.play_audio(file_path, call_id=call_id, loop=loop)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("play_audio failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def stop_audio(call_id: int | None = None) -> dict[str, Any]:
    """Stop audio playback and resume default Music-on-Hold.

    Args:
        call_id: Call ID (default: current active call)
    """
    assert call_mgr is not None
    try:
        call_mgr.stop_audio(call_id=call_id)
        return {"status": "ok"}
    except Exception as e:
        log.exception("stop_audio failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def conference(call_ids: list[int]) -> dict[str, Any]:
    """Bridge multiple active calls into a conference.

    Cross-connects audio of all specified calls so all parties hear each other.
    The calling UA acts as a conference bridge host.

    Args:
        call_ids: List of call IDs to bridge together
    """
    assert call_mgr is not None
    try:
        info = call_mgr.conference(call_ids)
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("conference failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: codec management
# ---------------------------------------------------------------------------
@mcp.tool()
async def set_codecs(
    codecs: list[str],
    call_id: int | None = None,
) -> dict[str, Any]:
    """Set codec priorities and optionally renegotiate an active call.

    Codecs are specified in priority order (first = highest). All unlisted
    codecs are disabled. If call_id is provided (or a call is active),
    sends a re-INVITE to renegotiate media with the new codec set.

    Args:
        codecs: Codec names in priority order, e.g. ["PCMU", "G722", "PCMA"]
        call_id: If set, renegotiate this active call with re-INVITE
    """
    assert engine is not None and call_mgr is not None
    try:
        if call_id is not None:
            info = call_mgr.reinvite_with_codecs(codecs, call_id=call_id)
        else:
            enabled = engine.set_codecs(codecs)
            info = {"codecs": enabled, "reinvite": False}
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("set_codecs failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def get_codecs() -> dict[str, Any]:
    """List all available codecs with their current priorities.

    Priority 0 means disabled. Higher priority = preferred in SDP negotiation.
    """
    assert engine is not None
    try:
        return {"codecs": engine.get_codecs()}
    except Exception as e:
        log.exception("get_codecs failed")
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


# ---------------------------------------------------------------------------
# Tool: call recordings
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_recording(call_id: int | None = None) -> dict[str, Any]:
    """Get the WAV recording file path for a call.

    Every call is automatically recorded to /recordings/call_{id}_{timestamp}.wav.
    Returns the file path, size, and duration so external tools can access the file.

    Args:
        call_id: Call ID to get recording for (default: current/last call)
    """
    assert call_mgr is not None
    try:
        info = call_mgr.get_call_info(call_id=call_id)
        rec_file = info.get("recording_file")
        if not rec_file:
            return {"status": "error", "error": "No recording available for this call"}

        from pathlib import Path
        path = Path(rec_file)
        if not path.exists():
            return {"status": "error", "error": f"Recording file not found: {rec_file}"}

        return {
            "recording_file": rec_file,
            "filename": path.name,
            "file_size": path.stat().st_size,
        }
    except Exception as e:
        log.exception("get_recording failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def list_recordings() -> dict[str, Any]:
    """List all call recording files in /recordings/.

    Returns recordings sorted by modification time (newest first).
    """
    from pathlib import Path
    recordings_dir = Path("/recordings")
    try:
        if not recordings_dir.exists():
            return {"recordings": [], "total_count": 0}
        files = sorted(
            recordings_dir.glob("call_*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        recordings = [
            {
                "filename": f.name,
                "file_path": str(f),
                "file_size": f.stat().st_size,
            }
            for f in files
        ]
        return {"recordings": recordings, "total_count": len(recordings)}
    except Exception as e:
        log.exception("list_recordings failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: call history
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_call_history(last_n: int | None = None) -> dict[str, Any]:
    """Get history of completed calls.

    Returns call records with remote URI, duration, status, codec, recording path.

    Args:
        last_n: Return only last N calls (default: all)
    """
    assert call_mgr is not None
    try:
        all_history = call_mgr.get_call_history()
        filtered = all_history[-last_n:] if last_n else all_history
        return {"history": filtered, "total_count": len(all_history)}
    except Exception as e:
        log.exception("get_call_history failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Tool: SIP instant messaging
# ---------------------------------------------------------------------------
@mcp.tool()
async def send_message(dest_uri: str, body: str) -> dict[str, Any]:
    """Send a SIP MESSAGE (instant text message).

    Args:
        dest_uri: Destination SIP URI (e.g. "sip:6002@asterisk")
        body: Message text content
    """
    assert account_mgr is not None
    try:
        account_mgr.send_message(dest_uri, body)
        return {"status": "ok", "dest_uri": dest_uri}
    except Exception as e:
        log.exception("send_message failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def get_messages(last_n: int | None = None) -> dict[str, Any]:
    """Get received SIP instant messages.

    Args:
        last_n: Return only last N messages (default: all)
    """
    assert account_mgr is not None
    try:
        msgs = account_mgr.get_messages(last_n=last_n)
        return {"messages": msgs, "total_count": len(msgs)}
    except Exception as e:
        log.exception("get_messages failed")
        return {"status": "error", "error": str(e)}


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
