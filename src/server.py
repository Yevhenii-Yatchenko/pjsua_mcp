"""PJSUA MCP Server — FastMCP entry point (multi-phone, dynamic tools)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# --- Protect MCP stdio channel from pjlib C-level console output ---
# pjlib writes directly to C fd 1 (stdout) regardless of consoleLevel during
# early init. We redirect fd 1 → stderr and give Python sys.stdout its own fd.
_mcp_stdout_fd = os.dup(1)          # save original stdout fd for MCP
os.dup2(2, 1)                       # C stdout (fd 1) → stderr
sys.stdout = os.fdopen(_mcp_stdout_fd, "w", buffering=1)  # line-buffered

import yaml
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel import NotificationOptions

from .sip_engine import SipEngine
from .sip_logger import PhoneMeta, filter_entries_by_owner
from .account_manager import PhoneRegistry, DEFAULT_PHONE_ID
from .call_manager import CallManager
from .pcap_manager import PcapManager
from .pcap_analyzer import analyze_pcap
from .phone_tool_factory import register_phone_tools, unregister_phone_tools
from .scenario_engine.event_bus import EventBus, set_default_bus
from .scenario_engine.orchestrator import run_scenario as run_scenario_impl

# All Python logging goes to stderr — stdout is the MCP channel
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("pjsua_mcp")

engine: SipEngine | None = None
registry: PhoneRegistry | None = None
call_mgr: CallManager | None = None
pcap_mgr: PcapManager | None = None
event_bus: EventBus | None = None
_poll_task: asyncio.Task | None = None

# Tracks dynamically-registered per-phone tool names so drop_phone can remove them.
_phone_tools: dict[str, list[str]] = {}

DEFAULT_PROFILE_PATH = "/config/phones.yaml"


async def _poll_pjsip_events(eng: SipEngine) -> None:
    """Background task: poll pjsip event loop from asyncio thread."""
    while True:
        try:
            eng.handle_events(10)
            if call_mgr:
                call_mgr.process_auto_answers()
                await call_mgr.process_auto_captures()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error in pjsip event poll")
        await asyncio.sleep(0.02)  # ~50 polls/sec


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage PJSUA2 lifecycle alongside MCP server."""
    global engine, registry, call_mgr, pcap_mgr, _poll_task, event_bus
    log.info("PJSUA MCP server starting")
    engine = SipEngine()
    registry = PhoneRegistry(engine)
    pcap_mgr = PcapManager()
    # pcap_mgr is passed into CallManager so SipCall can reference the
    # active capture when writing the .meta.json sidecar on disconnect.
    call_mgr = CallManager(engine, registry, pcap_mgr=pcap_mgr)

    # Scenario engine plumbing — bus is the default for pjsua callbacks to
    # emit events (reg.*, call.state.*, dtmf.*).
    event_bus = EventBus(loop=asyncio.get_running_loop())
    set_default_bus(event_bus)

    # Start engine up-front so add_phone works immediately.
    engine.initialize()

    # Pin every codec the per-phone SDP rewriter might need so media
    # activation never fails on "filter advertises codec X that endpoint
    # disabled". From here on, set_codecs() is for TEMPORARY overrides
    # only (e.g. mid-call re-INVITE in a scenario action). Per-phone
    # codec preferences are enforced via SipCall.onCallSdpCreated, not
    # endpoint state.
    engine.enable_audio_codec_superset()

    _poll_task = asyncio.create_task(_poll_pjsip_events(engine))

    yield

    log.info("PJSUA MCP server shutting down")
    set_default_bus(None)
    if _poll_task and not _poll_task.done():
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
    await pcap_mgr.cleanup()
    call_mgr.hangup_all()
    registry.drop_all()
    engine.shutdown()
    log.info("PJSUA MCP server stopped")


mcp = FastMCP("pjsua", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _notify_tools_changed() -> None:
    """Send notifications/tools/list_changed to the MCP client."""
    try:
        ctx = mcp.get_context()
        await ctx.session.send_tool_list_changed()
    except Exception:
        log.debug("send_tool_list_changed failed (context not ready?)")


def _validate_phone_id(phone_id: str) -> None:
    import re
    if not re.fullmatch(r"[a-z0-9_]{1,32}", phone_id):
        raise ValueError(
            f"phone_id {phone_id!r} invalid — must match ^[a-z0-9_]{{1,32}}$"
        )
    RESERVED_ACTIONS = {
        "make_call", "answer_call", "reject_call", "hangup",
        "get_call_info", "get_call_history", "list_calls", "get_active_calls",
        "send_dtmf", "hold", "unhold", "blind_transfer", "attended_transfer",
        "conference", "play_audio", "stop_audio", "get_recording",
        "send_message", "get_messages", "get_registration_status",
        "register", "unregister",
    }
    # Guard against phone_id producing a collision with static tool names.
    static_tool_names = {
        "add_phone", "drop_phone", "list_phones", "get_phone", "update_phone",
        "load_phones",
        "get_sip_log",
        "list_recordings",
    }
    for action in RESERVED_ACTIONS:
        if f"{phone_id}_{action}" in static_tool_names:
            raise ValueError(f"phone_id {phone_id!r} would collide with static tool")


def _add_phone_impl(
    phone_id: str,
    domain: str,
    username: str | None,
    password: str | None,
    *,
    realm: str | None = None,
    srtp: bool = False,
    auto_answer: bool = False,
    transport: str = "udp",
    local_port: int = 0,
    codecs: list[str] | None = None,
    register: bool = True,
    recording_enabled: bool = False,
    capture_enabled: bool = False,
) -> dict[str, Any]:
    """Common add-phone routine — no client notification, no async."""
    assert registry is not None and engine is not None

    _validate_phone_id(phone_id)

    # If this phone already exists, drop its tools first so we don't leave
    # orphans when PhoneRegistry.add_phone internally replaces the account.
    if phone_id in _phone_tools:
        unregister_phone_tools(mcp, _phone_tools.pop(phone_id))

    registry.add_phone(
        phone_id,
        domain=domain,
        username=username,
        password=password,
        realm=realm,
        srtp=srtp,
        auto_answer=auto_answer,
        transport=transport,
        local_port=local_port,
        codecs=codecs,
        register=register,
        recording_enabled=recording_enabled,
        capture_enabled=capture_enabled,
    )

    tool_names = register_phone_tools(mcp, phone_id, call_mgr, registry)
    _phone_tools[phone_id] = tool_names

    cfg = registry.get_config(phone_id)
    reg = registry.get_registration_info(phone_id)
    return {
        "phone_id": phone_id,
        "domain": domain,
        "username": username,
        "auto_answer": auto_answer,
        "transport": transport,
        "transport_id": cfg.transport_id if cfg else None,
        "codecs": cfg.codecs if cfg else codecs,
        "recording_enabled": cfg.recording_enabled if cfg else recording_enabled,
        "capture_enabled": cfg.capture_enabled if cfg else capture_enabled,
        "tools_registered": len(tool_names),
        **reg,
    }


def _drop_phone_impl(phone_id: str, *, hangup_calls: bool = True) -> dict[str, Any]:
    assert registry is not None and call_mgr is not None

    if not registry.has_phone(phone_id):
        return {"status": "error", "error": f"Phone {phone_id!r} not registered"}

    if hangup_calls:
        call_mgr.hangup_all(phone_id=phone_id)

    tool_names = _phone_tools.pop(phone_id, [])
    removed = unregister_phone_tools(mcp, tool_names) if tool_names else 0

    registry.drop_phone(phone_id)
    return {"status": "ok", "phone_id": phone_id, "tools_removed": removed}


# ---------------------------------------------------------------------------
# Phone CRUD (static)
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_phones() -> dict[str, Any]:
    """List all registered phones with their registration state and transport info."""
    assert registry is not None
    try:
        phones = registry.list_phones()
        # Enrich with active-call count per phone
        active_by_phone: dict[str, int] = {}
        if call_mgr is not None:
            for call in call_mgr.list_calls():
                pid = call.get("phone_id")
                if pid:
                    active_by_phone[pid] = active_by_phone.get(pid, 0) + 1
        for p in phones:
            p["active_call_count"] = active_by_phone.get(p["phone_id"], 0)
            p["tools"] = _phone_tools.get(p["phone_id"], [])
        return {"phones": phones, "total_count": len(phones)}
    except Exception as e:
        log.exception("list_phones failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def add_phone(
    phone_id: str,
    domain: str,
    username: str,
    password: str,
    transport: str = "udp",
    local_port: int = 0,
    codecs: list[str] | None = None,
    auto_answer: bool = False,
    srtp: bool = False,
    realm: str | None = None,
    register: bool = True,
    recording_enabled: bool = False,
    capture_enabled: bool = False,
) -> dict[str, Any]:
    """Add a new phone (SIP account) and register its per-phone action tools.

    After a successful add, new tools named `<phone_id>_make_call`,
    `<phone_id>_hangup`, etc. become visible via
    `notifications/tools/list_changed`.

    `codecs` (optional, e.g. `["PCMA", "telephone-event"]`) sets the
    phone's outbound SDP filter — every offer and 200-OK answer this
    phone produces will list ONLY these codecs. Media activation then
    picks from the SDP intersection, so RTP send/receive on this phone
    matches the list. `None` (default) keeps the global endpoint
    priorities (set via `set_codecs`). DTMF (`telephone-event`) is
    auto-preserved by the SDP rewriter even when not in the list.

    Recording defaults to OFF: pass `recording_enabled=True` (or set it
    in the YAML profile) to write every call on this phone to
    `/recordings/<phone_id>/call_<call_id>_<ts>.wav` alongside a
    `.meta.json` sidecar. Toggle at runtime via
    `update_phone(phone_id=..., recording_enabled=...)`.

    Auto-capture also defaults to OFF: `capture_enabled=True` opens a
    per-phone tcpdump subprocess on the first audio-active call and
    closes it on the last disconnect. The pcap lands in
    `/captures/<phone_id>/call_<call_id>_<ts>.pcap`, paired by basename
    with the matching recording. Toggle at runtime via
    `update_phone(phone_id=..., capture_enabled=...)`.
    """
    try:
        result = _add_phone_impl(
            phone_id=phone_id,
            domain=domain, username=username, password=password,
            realm=realm, srtp=srtp, auto_answer=auto_answer,
            transport=transport, local_port=local_port,
            codecs=codecs, register=register,
            recording_enabled=recording_enabled,
            capture_enabled=capture_enabled,
        )
        await asyncio.sleep(1.0)  # give REGISTER a moment
        result.update(registry.get_registration_info(phone_id))
        await _notify_tools_changed()
        return {"status": "ok", **result}
    except Exception as e:
        log.exception("add_phone failed")
        return {"status": "error", "error": str(e), "phone_id": phone_id}


@mcp.tool()
async def drop_phone(
    phone_id: str,
    hangup_calls: bool = True,
) -> dict[str, Any]:
    """Drop a phone: hang up its calls, unregister, close transport, unload tools."""
    try:
        result = _drop_phone_impl(phone_id, hangup_calls=hangup_calls)
        await _notify_tools_changed()
        return result
    except Exception as e:
        log.exception("drop_phone failed")
        return {"status": "error", "error": str(e), "phone_id": phone_id}


@mcp.tool()
async def get_phone(phone_id: str) -> dict[str, Any]:
    """Return detailed info for one phone — reg state, credentials, active calls, tools."""
    assert registry is not None
    try:
        cfg = registry.get_config(phone_id)
        if cfg is None:
            return {"status": "error", "error": f"Phone {phone_id!r} not registered"}
        reg = registry.get_registration_info(phone_id)
        active_calls = call_mgr.list_calls(phone_id=phone_id) if call_mgr else []
        return {
            "phone_id": phone_id,
            "domain": cfg.domain,
            "username": cfg.username,
            "realm": cfg.realm,
            "srtp": cfg.srtp,
            "auto_answer": cfg.auto_answer,
            "transport": cfg.transport,
            "local_port": cfg.local_port,
            "transport_id": cfg.transport_id,
            "codecs": cfg.codecs,
            "recording_enabled": cfg.recording_enabled,
            "capture_enabled": cfg.capture_enabled,
            "active_calls": active_calls,
            "tools": _phone_tools.get(phone_id, []),
            **reg,
        }
    except Exception as e:
        log.exception("get_phone failed")
        return {"status": "error", "error": str(e), "phone_id": phone_id}


@mcp.tool()
async def update_phone(
    phone_id: str,
    auto_answer: bool | None = None,
    codecs: list[str] | None = None,
    password: str | None = None,
    realm: str | None = None,
    srtp: bool | None = None,
    recording_enabled: bool | None = None,
    capture_enabled: bool | None = None,
) -> dict[str, Any]:
    """Mutate runtime-parameters of an existing phone.

    auto_answer — instantaneous.
    codecs — instantaneous; updates this phone's SDP-rewrite filter and
      sends a re-INVITE on every CONFIRMED call so the live media swaps
      codec. Non-CONFIRMED calls (CALLING / EARLY) are updated in place;
      their first SDP exchange already encodes the new filter. Held
      calls get re-INVITEd as sendrecv (effectively unhold) — to keep
      a hold, unhold the call first or update codecs after unhold.
      Affected call IDs are returned in `codec_reinvited_call_ids`.
    recording_enabled — instantaneous; flips recording on/off on every
      currently active call of this phone. off→on starts a new WAV (with
      a new microsecond-unique filename); on→off closes the current WAV
      and writes its `.meta.json` sidecar. A call can cycle through
      on→off→on→... any number of times before DISCONNECTED.
    capture_enabled — instantaneous auto-capture toggle. off→on during
      an active call queues a tcpdump start (captures from "now" on; no
      retroactive packets). on→off flushes and closes the current pcap
      and future calls won't open a new one while it's off.
    password / realm / srtp — force a fresh REGISTER cycle.
    """
    assert registry is not None and engine is not None and call_mgr is not None
    try:
        cfg = registry.get_config(phone_id)
        if cfg is None:
            return {"status": "error", "error": f"Phone {phone_id!r} not registered"}

        reregister_needed = False
        affected_call_ids: list[int] | None = None
        codec_reinvited_call_ids: list[int] | None = None
        if auto_answer is not None:
            cfg.auto_answer = auto_answer
        if codecs is not None:
            cfg.codecs = list(codecs)
            codec_reinvited_call_ids = call_mgr.set_codecs_for_phone(
                phone_id, cfg.codecs,
            )
        if recording_enabled is not None:
            cfg.recording_enabled = recording_enabled
            affected_call_ids = call_mgr.set_recording_enabled(phone_id, recording_enabled)
        if capture_enabled is not None:
            call_mgr.set_capture_enabled(phone_id, capture_enabled)
        if password is not None:
            cfg.password = password
            reregister_needed = True
        if realm is not None:
            cfg.realm = realm
            reregister_needed = True
        if srtp is not None:
            cfg.srtp = srtp
            reregister_needed = True

        if reregister_needed:
            registry.reregister_phone(phone_id)
            await asyncio.sleep(1.0)

        response: dict[str, Any] = {
            "status": "ok",
            "phone_id": phone_id,
            "reregistered": reregister_needed,
            "codecs": cfg.codecs,
            "recording_enabled": cfg.recording_enabled,
            "capture_enabled": cfg.capture_enabled,
            **registry.get_registration_info(phone_id),
        }
        if affected_call_ids is not None:
            response["affected_call_ids"] = affected_call_ids
        if codec_reinvited_call_ids is not None:
            response["codec_reinvited_call_ids"] = codec_reinvited_call_ids
        return response
    except Exception as e:
        log.exception("update_phone failed")
        return {"status": "error", "error": str(e), "phone_id": phone_id}


_PHONE_FIELDS = {
    "phone_id", "domain", "username", "password", "realm", "srtp",
    "auto_answer", "transport", "local_port", "codecs", "register",
    "recording_enabled", "capture_enabled",
}

# Keys we used to accept but no longer do. If they appear in the profile,
# warn and strip instead of hard-failing — so YAML files written against
# the previous commit still load.
_LEGACY_FIELDS = {"recordings_dir"}


def _load_profile_yaml(path: str) -> list[dict[str, Any]]:
    """Read a YAML profile and return a list of resolved phone specs.

    Top-level shape:
        defaults: {domain, password, codecs, ...}   # optional
        phones:
          - phone_id: a
            username: "1001"
          - ...

    `defaults` keys are merged into each phone (phone-level values override).
    Every phone must have `phone_id`, `domain`, `username`, `password` after
    the merge.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(
            f"Profile not found: {path}. See config/phones.example.yaml in "
            f"the repo for the template — copy it to ./config/phones.yaml on "
            f"the host (mounted read-only to /config in the container) and retry."
        )

    with file_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping, got {type(data).__name__}")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: 'defaults' must be a mapping")

    phones = data.get("phones") or []
    if not isinstance(phones, list):
        raise ValueError(f"{path}: 'phones' must be a list")
    if not phones:
        raise ValueError(f"{path}: 'phones' list is empty — nothing to load")

    resolved = []
    for i, entry in enumerate(phones):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: phones[{i}] must be a mapping")
        merged = {**defaults, **entry}
        legacy = set(merged) & _LEGACY_FIELDS
        if legacy:
            log.warning(
                "%s: phones[%d] uses deprecated keys %s — ignoring. "
                "Recording is now always-on and lands in /recordings/<phone_id>/.",
                path, i, sorted(legacy),
            )
            for key in legacy:
                merged.pop(key, None)
        unknown = set(merged) - _PHONE_FIELDS
        if unknown:
            raise ValueError(
                f"{path}: phones[{i}] has unknown keys: {sorted(unknown)}. "
                f"Allowed: {sorted(_PHONE_FIELDS)}"
            )
        for required in ("phone_id", "domain", "username", "password"):
            if not merged.get(required):
                raise ValueError(
                    f"{path}: phones[{i}] missing required field {required!r} "
                    f"(not provided directly and not inherited from defaults)"
                )
        resolved.append(merged)

    ids = [p["phone_id"] for p in resolved]
    if len(set(ids)) != len(ids):
        seen = set()
        dups = [x for x in ids if x in seen or seen.add(x)]
        raise ValueError(f"{path}: duplicate phone_id(s): {dups}")

    return resolved


@mcp.tool()
async def load_phones(
    path: str = DEFAULT_PROFILE_PATH,
    merge: bool = False,
) -> dict[str, Any]:
    """Load a YAML phone profile and apply it atomically.

    Default (`merge=False`): **replace** semantics — the profile becomes
    the full phone roster. Active calls on existing phones are hung up,
    existing phones are dropped (transports closed, tools unloaded), then
    the profile is loaded. YAML parse/validation happens first — on any
    error the existing state is untouched.

    With `merge=True`: upsert — only phone_ids present in the profile are
    (re)created; phones not mentioned in the profile are left alone.

    The file is read from inside the container (the host's `./config/`
    directory is mounted to `/config` read-only in docker-compose.yml).

    YAML shape:
        defaults:                    # optional — merged into every phone
          domain: sip.example.com
          password: xxx
          codecs: [PCMA]
          auto_answer: false
        phones:
          - phone_id: a
            username: "1001"
            codecs: [PCMU, telephone-event]   # phone-level override wins
          - phone_id: b
            username: "1002"
            auto_answer: true

    A single `tools/list_changed` notification fires after all changes.

    Args:
        path: Profile path as seen from the container (default: /config/phones.yaml).
        merge: If True, keep phones not listed in the profile. Default False = replace.
    """
    assert registry is not None and call_mgr is not None

    # 1. Parse + validate BEFORE touching any state — fail fast.
    try:
        specs = _load_profile_yaml(path)
    except Exception as e:
        log.exception("load_phones: parse failed")
        return {"status": "error", "error": str(e), "path": path}

    dropped: list[str] = []
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # 2. In replace mode, tear down all existing phones + their calls first.
    if not merge:
        existing_ids = list(registry.list_phone_ids())
        if existing_ids:
            call_mgr.hangup_all()  # cancel every phone's active calls
            # Give pjsip a moment to emit BYE before we rip the accounts out.
            await asyncio.sleep(0.3)
            for pid in existing_ids:
                try:
                    _drop_phone_impl(pid, hangup_calls=True)
                    dropped.append(pid)
                except Exception as e:
                    log.exception("load_phones: drop %s failed", pid)
                    errors.append({"phone_id": pid, "error": f"drop failed: {e}"})

    # 3. Add phones from the profile. _add_phone_impl handles per-phone_id
    # replacement too, so merge mode still overwrites matching phone_ids.
    for spec in specs:
        try:
            res = _add_phone_impl(**spec)
            results.append(res)
        except Exception as e:
            log.exception("load_phones[%s] failed", spec.get("phone_id"))
            errors.append({"phone_id": spec.get("phone_id"), "error": str(e)})

    # 4. Let REGISTER responses land, then refresh registration state in the report.
    await asyncio.sleep(1.5)
    for r in results:
        r.update(registry.get_registration_info(r["phone_id"]))

    # 5. Single tools/list_changed notification covering the whole replace.
    await _notify_tools_changed()

    return {
        "status": "ok" if not errors else "partial",
        "path": path,
        "mode": "merge" if merge else "replace",
        "dropped": dropped,
        "added": results,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# SIP log (global with optional per-phone / per-call filter)
# ---------------------------------------------------------------------------
def _build_phones_meta() -> dict[str, PhoneMeta]:
    """Aggregate the live per-phone identity bundle used by ownership-based
    log filtering. Reads PhoneRegistry + SipEngine + CallManager state.
    """
    assert registry is not None and engine is not None and call_mgr is not None

    sip_index = call_mgr.get_sip_call_id_index()
    by_phone: dict[str, dict[str, set[str]]] = {}
    for sip_id, info in sip_index.items():
        ph = info.get("phone_id")
        if not ph:
            continue
        slot = by_phone.setdefault(
            ph, {"sip_call_ids": set(), "remote_uris": set()}
        )
        slot["sip_call_ids"].add(sip_id)
        if info.get("remote_uri"):
            slot["remote_uris"].add(info["remote_uri"])

    metas: dict[str, PhoneMeta] = {}
    for ph_id in registry.list_phone_ids():
        cfg = registry.get_config(ph_id)
        local_port = (
            engine.get_transport_port(cfg.transport_id)
            if cfg and cfg.transport_id is not None
            else None
        )
        slot = by_phone.get(ph_id, {})
        metas[ph_id] = PhoneMeta(
            phone_id=ph_id,
            username=cfg.username if cfg else None,
            local_port=local_port,
            sip_call_ids=slot.get("sip_call_ids", set()),
            remote_uris=slot.get("remote_uris", set()),
        )
    return metas


def _resolve_sip_call_id(phone_id: str | None, call_id: int) -> str | None:
    """Look up the SIP Call-ID header value for a pjsua_call_id. None if
    the tracker has no entry for this call (e.g. call already gone +
    historical purge)."""
    assert call_mgr is not None
    sip_index = call_mgr.get_sip_call_id_index()
    for sip_id, info in sip_index.items():
        if info.get("pjsua_call_id") != call_id:
            continue
        if phone_id is not None and info.get("phone_id") != phone_id:
            continue
        return sip_id
    return None


@mcp.tool()
async def get_sip_log(
    last_n: int | None = None,
    filter_text: str | None = None,
    phone_id: str | None = None,
    call_id: int | None = None,
) -> dict[str, Any]:
    """Retrieve pjsua SIP log entries.

    `phone_id` — filter to entries owned by that phone. Ownership is
      resolved structurally: SIP Call-ID matches a tracked call, or the
      message's Via line carries the phone's local transport port, or
      it's a REGISTER from the phone's username. Replaces the previous
      naive substring filter (which produced cross-leg false positives
      — e.g. bob's `From: <sip:alice@>` looked like alice's traffic).

    `call_id` — pjsua-internal call id (int). Restricts to the SIP
      dialog of that call (matched on Call-ID header). Combinable with
      `phone_id`.

    `filter_text` — substring filter applied before the ownership pass.

    `last_n` — return only the last N entries after filtering.

    When ownership cannot be resolved structurally for some entries but a
    substring of the phone's username appears in them, those are still
    kept and the response includes a `warning` field flagging the count.
    """
    assert engine is not None and registry is not None and call_mgr is not None
    try:
        entries = engine.get_log_entries(last_n=None, filter_text=filter_text)
        warning: str | None = None

        if phone_id is not None or call_id is not None:
            target_sip_call_id: str | None = None
            if call_id is not None:
                target_sip_call_id = _resolve_sip_call_id(phone_id, call_id)
                if target_sip_call_id is None:
                    return {
                        "entries": [],
                        "total_count": 0,
                        "warning": (
                            f"call_id={call_id} unknown to tracker — "
                            "either it never existed or its mapping was "
                            "purged. No entries returned."
                        ),
                    }

            phones_meta = _build_phones_meta()
            entries, fallback_count = filter_entries_by_owner(
                entries,
                phones=phones_meta,
                target_phone=phone_id,
                target_sip_call_id=target_sip_call_id,
            )
            if fallback_count > 0:
                warning = (
                    f"fallback to substring match for {fallback_count} "
                    "entries (no structural ownership signal — likely "
                    "historical entries from before tracker init)"
                )

        if last_n is not None:
            entries = entries[-last_n:]

        result: dict[str, Any] = {
            "entries": entries,
            "total_count": len(entries),
        }
        if warning:
            result["warning"] = warning
        return result
    except Exception as e:
        log.exception("get_sip_log failed")
        return {"status": "error", "error": str(e)}


_RECORDINGS_ROOT = Path("/recordings")
_CAPTURES_ROOT = Path("/captures")


def _parse_recording_filename(path: Path) -> tuple[str | None, int | None]:
    """Infer (phone_id, call_id) from a recording's path + filename.

    New layout — `/recordings/<phone_id>/call_<call_id>_<ts>.wav`:
      * phone_id comes from the parent directory name;
      * call_id from stem parts[1].

    Legacy flat layout — `/recordings/call_<phone_id>_<call_id>_<ts>.wav`:
      * phone_id from stem parts[1];
      * call_id from stem parts[2].

    Anything that doesn't match returns (None, None).
    """
    stem = path.stem  # e.g. "call_0_20260101_120000" or "call_a_0_20260101_120000"
    parts = stem.split("_")
    if len(parts) < 2 or parts[0] != "call":
        return None, None

    parent = path.parent
    if parent.name and parent != _RECORDINGS_ROOT:
        # new layout — phone_id in dir name
        try:
            return parent.name, int(parts[1])
        except ValueError:
            return parent.name, None
    # legacy flat layout — phone_id embedded after "call_"
    if len(parts) >= 3:
        try:
            return parts[1], int(parts[2])
        except ValueError:
            return parts[1], None
    return None, None


@mcp.tool()
async def list_recordings(
    phone_id: str | None = None,
    call_id: int | None = None,
) -> dict[str, Any]:
    """List every WAV under /recordings/, optionally filtered by phone/call.

    Recording is always-on — every call lands in
    `/recordings/<phone_id>/call_<call_id>_<ts>.wav` along with a
    `.meta.json` sidecar. This tool also surfaces pre-refactor flat files
    (`call_<phone_id>_<call_id>_<ts>.wav` directly under `/recordings/`)
    so historical data stays visible.
    """
    try:
        if not _RECORDINGS_ROOT.exists():
            return {"recordings": [], "total_count": 0, "root": str(_RECORDINGS_ROOT)}

        recordings: list[dict[str, Any]] = []
        for f in _RECORDINGS_ROOT.rglob("*.wav"):
            if not f.is_file():
                continue
            file_phone, file_call = _parse_recording_filename(f)
            if phone_id is not None and file_phone != phone_id:
                continue
            if call_id is not None and file_call != call_id:
                continue
            meta_path = f.with_suffix(".meta.json")
            recordings.append({
                "filename": f.name,
                "file_path": str(f),
                "file_size": f.stat().st_size,
                "phone_id": file_phone,
                "call_id": file_call,
                "meta_path": str(meta_path) if meta_path.exists() else None,
            })

        recordings.sort(key=lambda r: r["file_path"], reverse=True)
        return {
            "recordings": recordings,
            "total_count": len(recordings),
            "root": str(_RECORDINGS_ROOT),
        }
    except Exception as e:
        log.exception("list_recordings failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Pcap analysis — turn a recorded /captures/<phone>/...pcap into a
# structured RTP/RTCP flow report.
# ---------------------------------------------------------------------------
def _resolve_pcap_path(phone_id: str, call_id: int | None) -> Path | None:
    """Find the matching pcap under `/captures/<phone_id>/`.

    `call_id=None` → most recent pcap (any name) for the phone.
    `call_id=N`    → most recent pcap whose stem starts with `call_<N>_`.

    Returns None if no match.
    """
    phone_dir = _CAPTURES_ROOT / phone_id
    if not phone_dir.is_dir():
        return None
    if call_id is None:
        candidates = list(phone_dir.glob("*.pcap"))
    else:
        candidates = list(phone_dir.glob(f"call_{call_id}_*.pcap"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _lookup_meta_for_pcap(pcap_path: Path) -> dict[str, Any] | None:
    """Find the recording meta.json whose `pcap` field equals `pcap_path`.

    Recordings live under `/recordings/<phone_id>/`; the pcap is in
    `/captures/<phone_id>/`. Their basenames differ (microsecond timestamps
    are taken at independent moments), so we walk the recordings dir for
    the same phone and match by the absolute pcap path stored in each
    sidecar.
    """
    target = str(pcap_path)
    rec_dir = _RECORDINGS_ROOT / pcap_path.parent.name
    if not rec_dir.is_dir():
        return None
    for meta_file in rec_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("pcap") == target:
            return data
    return None


@mcp.tool()
async def analyze_capture(
    phone_id: str,
    call_id: int | None = None,
) -> dict[str, Any]:
    """Parse a phone's pcap file into a structured RTP/RTCP flow report.

    Resolves the pcap under `/captures/<phone_id>/`:
      * `call_id=None` → most recent pcap (latest by mtime);
      * `call_id=<N>` → newest pcap whose name starts with `call_<N>_`.

    Returns aggregated `rtp_flows` and `rtcp_flows` (one entry per
    distinct `(src_port, dst_port, payload_type)` tuple) plus a
    per-phone summary (`phone_rtp_port`, `phone_rtp_codecs_seen`,
    `non_phone_codecs_on_phone_port`).

    Per-phone fields are populated when the call's recording sidecar
    `.meta.json` includes `local_rtp_port` (recorded automatically for
    new calls — old fixtures lacking the field leave per-phone fields
    as None/[]). Codec leak detection compares observed codecs against
    the phone's configured `codecs` list (`add_phone(codecs=...)` /
    `update_phone(codecs=...)`); a non-empty
    `non_phone_codecs_on_phone_port` indicates the per-phone SDP filter
    is leaking.

    Errors are returned as `{error: "..."}` rather than exceptions:
    missing pcap (`capture_enabled: false` for the call), unknown
    libpcap linktype, unreadable file. `total_packets`, `rtp_flows`,
    `rtcp_flows` are still populated where possible.
    """
    assert registry is not None
    try:
        pcap_path = _resolve_pcap_path(phone_id, call_id)
        if pcap_path is None:
            return {
                "phone_id": phone_id,
                "call_id": call_id,
                "path": None,
                "error": (
                    f"no capture for phone={phone_id!r} call_id={call_id} — "
                    "is `capture_enabled` set on the phone?"
                ),
                "total_packets": 0,
                "rtp_flows": [],
                "rtcp_flows": [],
                "phone_rtp_port": None,
                "phone_rtp_codecs_seen": None,
                "non_phone_codecs_on_phone_port": None,
            }

        meta = _lookup_meta_for_pcap(pcap_path)
        local_rtp_port = meta.get("local_rtp_port") if meta else None
        cfg = registry.get_config(phone_id)
        expected_codecs = cfg.codecs if cfg else None

        result = analyze_pcap(
            pcap_path,
            phone_rtp_port=local_rtp_port,
            expected_codecs=expected_codecs,
        )
        result["phone_id"] = phone_id
        result["call_id"] = call_id if call_id is not None else (
            meta.get("call_id") if meta else None
        )
        return result
    except Exception as e:
        log.exception("analyze_capture failed")
        return {
            "status": "error",
            "error": str(e),
            "phone_id": phone_id,
            "call_id": call_id,
        }


# ---------------------------------------------------------------------------
# Scenario engine tools — validate_scenario / run_scenario
# ---------------------------------------------------------------------------
@mcp.tool()
async def validate_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    """Dry-run check for a scenario — catch typos / bad events / unknown actions
    BEFORE actually running. Returns `{status: "ok" | "error", issues: [...]}`.

    Does NOT touch pjsua / Asterisk. Purely static checks:
      - `hooks[*].when` uses a known event-type prefix
      - `stop_on[*].event` is a known type
      - no `action:` name that's not in the executor's dispatch table
    """
    from .scenario_engine.validator import validate_scenario as _validate

    if not isinstance(scenario, dict):
        return {
            "status": "error",
            "issues": [{"kind": "arg",
                        "msg": f"scenario must be a dict, got {type(scenario).__name__}"}],
        }
    return _validate(scenario)


@mcp.tool()
async def run_scenario(
    scenario: dict[str, Any],
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Run a scenario (must be a dict — full inline scenario specification).

    Returns a Timeline dict with `status`, `elapsed_ms`, `timeline`, `errors`.
    The call blocks until the scenario either matches a `stop_on` condition
    or hits `timeout_ms` (defaults to scenario value or 60_000 if neither
    provided).
    """
    if event_bus is None or call_mgr is None or registry is None:
        return {"status": "error", "error": "scenario engine not initialised"}
    if not isinstance(scenario, dict):
        return {
            "status": "error",
            "error": f"scenario must be a dict, got {type(scenario).__name__}",
        }
    scn_input: dict[str, Any] = scenario
    if timeout_ms is not None:
        scn_input = {**scenario, "timeout_ms": int(timeout_ms)}
    try:
        loop = asyncio.get_running_loop()
        result = await run_scenario_impl(
            scn_input,
            bus=event_bus,
            call_manager=call_mgr,
            registry=registry,
            loop=loop,
            engine=engine,
        )
    except Exception as e:
        log.exception("run_scenario failed")
        return {"status": "error", "error": str(e)}
    return result.to_dict()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    # Enable tools_changed capability so the MCP client honours
    # notifications/tools/list_changed events after add_phone/drop_phone.
    _orig_init_options = mcp._mcp_server.create_initialization_options

    def _create_with_tools_changed(
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict | None = None,
    ):
        return _orig_init_options(
            notification_options=NotificationOptions(tools_changed=True),
            experimental_capabilities=experimental_capabilities,
        )

    mcp._mcp_server.create_initialization_options = _create_with_tools_changed
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
