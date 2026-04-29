"""Dynamic per-phone tool factory.

Each registered phone gets ~22 MCP tools named `<phone_id>_<action>` — these
are added/removed at runtime via `mcp.add_tool()` / `mcp.remove_tool()` and the
client is notified with `notifications/tools/list_changed`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .call_manager import CallManager
from .account_manager import PhoneRegistry
from .scenario_engine.artifacts import external_path

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

log = logging.getLogger(__name__)

_RECORDINGS_ROOT = Path("/recordings")


def _error_response(action: str, phone_id: str, exc: Exception) -> dict[str, Any]:
    log.exception("[%s] %s failed", phone_id, action)
    return {"status": "error", "error": str(exc), "phone_id": phone_id}


def _externalize_recording_file(
    info: dict[str, Any], host_recordings_root: str | None,
) -> dict[str, Any]:
    """Mutate `info` so its `recording_file` (if any) is a client-readable
    path. Returns the same dict for chaining."""
    rec = info.get("recording_file")
    if rec:
        info["recording_file"] = external_path(
            rec, _RECORDINGS_ROOT, host_recordings_root,
        )
    return info


def register_phone_tools(
    mcp: "FastMCP",
    phone_id: str,
    call_mgr: CallManager,
    registry: PhoneRegistry,
    host_recordings_root: str | None = None,
) -> list[str]:
    """Register the per-phone tool set and return the list of registered names.

    `host_recordings_root` (when given) re-anchors every `recording_file`
    / `meta_path` field surfaced by these tools (`get_recording`,
    `get_call_info`, `get_active_calls`, `get_call_history`, plus the
    `make_call` / `answer_call` / `*_transfer` info responses) so
    out-of-container clients can Read the WAV/sidecar without resolving
    the bind mount themselves. When None, container paths come back
    unchanged — still works in-container.
    """
    names: list[str] = []

    def _add(fn: Any, action: str, description: str) -> None:
        tool_name = f"{phone_id}_{action}"
        mcp.add_tool(fn, name=tool_name, description=description)
        names.append(tool_name)

    # ------------------------------------------------------------------
    # Call lifecycle
    # ------------------------------------------------------------------
    async def make_call(
        dest_uri: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            info = call_mgr.make_call(dest_uri, phone_id=phone_id, headers=headers)
            _externalize_recording_file(info, host_recordings_root)
            return {"status": "ok", **info}
        except Exception as e:
            return _error_response("make_call", phone_id, e)

    _add(make_call, "make_call", f"Place an outbound SIP call from phone {phone_id!r}.")

    async def answer_call(
        call_id: int | None = None,
        status_code: int = 200,
    ) -> dict[str, Any]:
        try:
            info = call_mgr.answer_call(phone_id=phone_id, call_id=call_id, status_code=status_code)
            _externalize_recording_file(info, host_recordings_root)
            return {"status": "ok", **info}
        except Exception as e:
            return _error_response("answer_call", phone_id, e)

    _add(answer_call, "answer_call", f"Answer an incoming call on phone {phone_id!r}.")

    async def reject_call(
        call_id: int | None = None,
        status_code: int = 486,
    ) -> dict[str, Any]:
        try:
            info = call_mgr.reject_call(phone_id=phone_id, call_id=call_id, status_code=status_code)
            _externalize_recording_file(info, host_recordings_root)
            return {"status": "ok", **info}
        except Exception as e:
            return _error_response("reject_call", phone_id, e)

    _add(reject_call, "reject_call", f"Reject an incoming call on phone {phone_id!r} (486/603/480).")

    async def hangup(call_id: int | None = None) -> dict[str, Any]:
        try:
            call_mgr.hangup(phone_id=phone_id, call_id=call_id)
            return {"status": "ok", "phone_id": phone_id, "call_id": call_id}
        except Exception as e:
            return _error_response("hangup", phone_id, e)

    _add(hangup, "hangup", f"Hang up a call on phone {phone_id!r}.")

    async def get_call_info(call_id: int | None = None) -> dict[str, Any]:
        try:
            info = call_mgr.get_call_info(phone_id=phone_id, call_id=call_id)
            return _externalize_recording_file(info, host_recordings_root)
        except Exception as e:
            return _error_response("get_call_info", phone_id, e)

    _add(get_call_info, "get_call_info", f"Get call state + RTP stats for phone {phone_id!r}.")

    # ------------------------------------------------------------------
    # Call listings
    # ------------------------------------------------------------------
    async def list_calls() -> dict[str, Any]:
        try:
            calls = call_mgr.list_calls(phone_id=phone_id)
            for c in calls:
                _externalize_recording_file(c, host_recordings_root)
            return {"phone_id": phone_id, "calls": calls, "total_count": len(calls)}
        except Exception as e:
            return _error_response("list_calls", phone_id, e)

    _add(list_calls, "list_calls", f"List all tracked calls on phone {phone_id!r}.")

    async def get_active_calls() -> dict[str, Any]:
        try:
            calls = call_mgr.get_active_calls(phone_id=phone_id)
            for c in calls:
                _externalize_recording_file(c, host_recordings_root)
            return {"phone_id": phone_id, "calls": calls, "active_count": len(calls)}
        except Exception as e:
            return _error_response("get_active_calls", phone_id, e)

    _add(get_active_calls, "get_active_calls", f"List active (non-DISCONNECTED) calls for phone {phone_id!r}.")

    async def get_call_history(last_n: int | None = None) -> dict[str, Any]:
        try:
            history = call_mgr.get_call_history(phone_id=phone_id, last_n=last_n)
            for h in history:
                _externalize_recording_file(h, host_recordings_root)
            return {"phone_id": phone_id, "history": history, "total_count": len(history)}
        except Exception as e:
            return _error_response("get_call_history", phone_id, e)

    _add(get_call_history, "get_call_history", f"Get call history for phone {phone_id!r}.")

    # ------------------------------------------------------------------
    # Call features
    # ------------------------------------------------------------------
    async def hold(call_id: int) -> dict[str, Any]:
        try:
            call_mgr.hold(call_id=call_id, phone_id=phone_id)
            return {"status": "ok", "phone_id": phone_id, "call_id": call_id}
        except Exception as e:
            return _error_response("hold", phone_id, e)

    _add(hold, "hold", f"Put a call on hold (re-INVITE sendonly) on phone {phone_id!r}.")

    async def unhold(call_id: int) -> dict[str, Any]:
        try:
            call_mgr.unhold(call_id=call_id, phone_id=phone_id)
            return {"status": "ok", "phone_id": phone_id, "call_id": call_id}
        except Exception as e:
            return _error_response("unhold", phone_id, e)

    _add(unhold, "unhold", f"Resume a held call on phone {phone_id!r}.")

    async def send_dtmf(call_id: int, digits: str) -> dict[str, Any]:
        try:
            call_mgr.send_dtmf(call_id=call_id, digits=digits, phone_id=phone_id)
            return {"status": "ok", "phone_id": phone_id, "call_id": call_id, "digits": digits}
        except Exception as e:
            return _error_response("send_dtmf", phone_id, e)

    _add(send_dtmf, "send_dtmf", f"Send DTMF digits on phone {phone_id!r} call.")

    async def blind_transfer(
        dest_uri: str,
        call_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            info = call_mgr.blind_transfer(dest_uri, phone_id=phone_id, call_id=call_id)
            _externalize_recording_file(info, host_recordings_root)
            return {"status": "ok", **info}
        except Exception as e:
            return _error_response("blind_transfer", phone_id, e)

    _add(blind_transfer, "blind_transfer", f"REFER a call from phone {phone_id!r} to dest_uri.")

    async def attended_transfer(
        call_id: int | None = None,
        dest_call_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            info = call_mgr.attended_transfer(
                phone_id=phone_id, call_id=call_id, dest_call_id=dest_call_id,
            )
            _externalize_recording_file(info, host_recordings_root)
            return {"status": "ok", **info}
        except Exception as e:
            return _error_response("attended_transfer", phone_id, e)

    _add(
        attended_transfer,
        "attended_transfer",
        f"REFER+Replaces on phone {phone_id!r} — both legs must belong to this phone.",
    )

    async def conference(call_ids: list[int]) -> dict[str, Any]:
        try:
            info = call_mgr.conference(call_ids=call_ids, phone_id=phone_id)
            return {"status": "ok", **info}
        except Exception as e:
            return _error_response("conference", phone_id, e)

    _add(conference, "conference", f"Bridge calls owned by phone {phone_id!r} into a conference.")

    # ------------------------------------------------------------------
    # Audio / recording
    # ------------------------------------------------------------------
    async def play_audio(
        file_path: str,
        call_id: int | None = None,
        loop: bool = False,
    ) -> dict[str, Any]:
        try:
            info = call_mgr.play_audio(
                file_path, phone_id=phone_id, call_id=call_id, loop=loop,
            )
            return {"status": "ok", **info}
        except Exception as e:
            return _error_response("play_audio", phone_id, e)

    _add(play_audio, "play_audio", f"Play a WAV into a call on phone {phone_id!r}.")

    async def stop_audio(call_id: int | None = None) -> dict[str, Any]:
        try:
            call_mgr.stop_audio(phone_id=phone_id, call_id=call_id)
            return {"status": "ok", "phone_id": phone_id, "call_id": call_id}
        except Exception as e:
            return _error_response("stop_audio", phone_id, e)

    _add(stop_audio, "stop_audio", f"Resume MOH on a call on phone {phone_id!r}.")

    async def get_recording(call_id: int | None = None) -> dict[str, Any]:
        try:
            info = call_mgr.get_call_info(phone_id=phone_id, call_id=call_id)
            cfg = registry.get_config(phone_id)
            rec_enabled = cfg.recording_enabled if cfg else False
            rec_file = info.get("recording_file")
            if not rec_file:
                return {
                    "status": "error",
                    "error": "No recording currently active for this call",
                    "phone_id": phone_id,
                    "recording_enabled": rec_enabled,
                }
            path = Path(rec_file)
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"Recording file not found: {rec_file}",
                    "phone_id": phone_id,
                    "recording_enabled": rec_enabled,
                }
            meta_path = path.with_suffix(".meta.json")
            return {
                "phone_id": phone_id,
                "recording_enabled": rec_enabled,
                "recording_file": external_path(
                    path, _RECORDINGS_ROOT, host_recordings_root,
                ),
                "filename": path.name,
                "file_size": path.stat().st_size,
                "meta_path": external_path(
                    meta_path, _RECORDINGS_ROOT, host_recordings_root,
                ) if meta_path.exists() else None,
            }
        except Exception as e:
            return _error_response("get_recording", phone_id, e)

    _add(get_recording, "get_recording",
         f"Get the LATEST active WAV for a call on phone {phone_id!r}. "
         f"A single call can have multiple WAVs when recording_enabled "
         f"toggles mid-call — use `list_recordings(phone_id=..., call_id=...)` "
         f"to see every segment.")

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    async def send_message(dest_uri: str, body: str) -> dict[str, Any]:
        try:
            registry.send_message(dest_uri, body, phone_id=phone_id)
            return {"status": "ok", "phone_id": phone_id, "dest_uri": dest_uri}
        except Exception as e:
            return _error_response("send_message", phone_id, e)

    _add(send_message, "send_message", f"Send SIP MESSAGE from phone {phone_id!r}.")

    async def get_messages(last_n: int | None = None) -> dict[str, Any]:
        try:
            msgs = registry.get_messages(phone_id=phone_id, last_n=last_n)
            return {"phone_id": phone_id, "messages": msgs, "total_count": len(msgs)}
        except Exception as e:
            return _error_response("get_messages", phone_id, e)

    _add(get_messages, "get_messages", f"Get SIP MESSAGE inbox for phone {phone_id!r}.")

    # ------------------------------------------------------------------
    # Registration / lifecycle (per-phone convenience)
    # ------------------------------------------------------------------
    async def get_registration_status() -> dict[str, Any]:
        try:
            return {"phone_id": phone_id, **registry.get_registration_info(phone_id)}
        except Exception as e:
            return _error_response("get_registration_status", phone_id, e)

    _add(get_registration_status, "get_registration_status",
         f"Get registration state of phone {phone_id!r}.")

    async def unregister() -> dict[str, Any]:
        try:
            registry.unregister_phone(phone_id)
            return {"status": "ok", "phone_id": phone_id}
        except Exception as e:
            return _error_response("unregister", phone_id, e)

    _add(unregister, "unregister",
         f"Send de-REGISTER for phone {phone_id!r} (keep account in registry).")

    async def register() -> dict[str, Any]:
        try:
            registry.reregister_phone(phone_id)
            await asyncio.sleep(1.0)  # let REGISTER 200 OK land
            return {"status": "ok", "phone_id": phone_id, **registry.get_registration_info(phone_id)}
        except Exception as e:
            return _error_response("register", phone_id, e)

    _add(register, "register",
         f"Force a fresh REGISTER cycle on phone {phone_id!r} — drops the current "
         f"pj.Account and recreates it, so the registrar sees a brand-new binding. "
         f"Use after a mu-bundle/edgeproxy restart or when a_get_registration_status "
         f"claims active but the real binding is stale.")

    log.info("[%s] Registered %d per-phone tools", phone_id, len(names))
    return names


def unregister_phone_tools(mcp: "FastMCP", tool_names: list[str]) -> int:
    """Remove per-phone tools previously registered via `register_phone_tools`.

    Returns the number of tools actually removed.
    """
    removed = 0
    for name in tool_names:
        try:
            mcp.remove_tool(name)
            removed += 1
        except Exception:
            log.debug("remove_tool(%r) failed — tool not registered?", name)
    log.info("Removed %d/%d phone tools", removed, len(tool_names))
    return removed
