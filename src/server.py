"""PJSUA MCP Server — FastMCP entry point (multi-phone, dynamic tools)."""

from __future__ import annotations

import asyncio
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
from .account_manager import PhoneRegistry, DEFAULT_PHONE_ID
from .call_manager import CallManager
from .pcap_manager import PcapManager
from .phone_tool_factory import register_phone_tools, unregister_phone_tools

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
_poll_task: asyncio.Task | None = None

# Tracks dynamically-registered per-phone tool names so drop_phone can remove them.
_phone_tools: dict[str, list[str]] = {}

DEFAULT_PROFILE_PATH = "/config/phones.yaml"
EXAMPLE_PROFILE_PATH = "/config/phones.example.yaml"

# Authoritative template returned by `get_phone_profile_example`. Kept here
# (not in a separate file) so the MCP tool is self-contained — callers don't
# need filesystem access to the repo to discover the schema.
PHONE_PROFILE_TEMPLATE = """\
# Phone profile for load_phone_profile.
#
# Save this file on the host at ./config/phones.yaml (the docker-compose
# bind mounts ./config to /config inside the container), fill in the
# credentials for your SIP stand, then invoke the MCP tool:
#
#   mcp__pjsua__load_phone_profile()                               # /config/phones.yaml
#   mcp__pjsua__load_phone_profile(path="/config/other.yaml")      # another profile
#
# Recording is always on. Calls land in /recordings/<phone_id>/ as
# call_<call_id>_<ts>.wav plus a .meta.json sidecar with codec, duration,
# remote URI, direction, and (if a capture is running) the paired pcap path.

# Keys under `defaults` are merged into every phone entry.
# Phone-level keys win over defaults.
defaults:
  domain: sip.example.com
  password: change_me
  codecs: [PCMA]
  transport: udp
  auto_answer: false
  register: true
  srtp: false
  # recording_enabled: true    # default — set false to suppress recording
                               # for every phone that doesn't override it

phones:
  - phone_id: a
    username: "1001"

  - phone_id: b
    username: "1002"
    auto_answer: true

  - phone_id: c
    username: "1003"
    auto_answer: true
    # recording_enabled: false  # per-phone override wins over defaults
    # Per-phone override:
    # password: "c_specific_pw"
    # realm: "example.realm"
"""


async def _poll_pjsip_events(eng: SipEngine) -> None:
    """Background task: poll pjsip event loop from asyncio thread."""
    while True:
        try:
            eng.handle_events(10)
            if call_mgr:
                call_mgr.process_auto_answers()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error in pjsip event poll")
        await asyncio.sleep(0.02)  # ~50 polls/sec


@asynccontextmanager
async def lifespan(server: FastMCP):
    """Manage PJSUA2 lifecycle alongside MCP server."""
    global engine, registry, call_mgr, pcap_mgr, _poll_task
    log.info("PJSUA MCP server starting")
    engine = SipEngine()
    registry = PhoneRegistry(engine)
    pcap_mgr = PcapManager()
    # pcap_mgr is passed into CallManager so SipCall can reference the
    # active capture when writing the .meta.json sidecar on disconnect.
    call_mgr = CallManager(engine, registry, pcap_mgr=pcap_mgr)

    # Start engine up-front so add_phone works immediately.
    engine.initialize()
    _poll_task = asyncio.create_task(_poll_pjsip_events(engine))

    yield

    log.info("PJSUA MCP server shutting down")
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
        "load_phone_profile", "get_phone_profile_example",
        "get_codecs", "set_codecs", "get_sip_log",
        "start_capture", "stop_capture", "get_pcap", "list_recordings",
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
    recording_enabled: bool = True,
) -> dict[str, Any]:
    """Common add-phone routine — no client notification, no async."""
    assert registry is not None and engine is not None

    _validate_phone_id(phone_id)

    # If this phone already exists, drop its tools first so we don't leave
    # orphans when PhoneRegistry.add_phone internally replaces the account.
    if phone_id in _phone_tools:
        unregister_phone_tools(mcp, _phone_tools.pop(phone_id))

    # Optionally apply endpoint-wide codec priorities before REGISTER.
    if codecs:
        engine.set_codecs(codecs)

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
        "recording_enabled": cfg.recording_enabled if cfg else recording_enabled,
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
    recording_enabled: bool = True,
) -> dict[str, Any]:
    """Add a new phone (SIP account) and register its per-phone action tools.

    After a successful add, new tools named `<phone_id>_make_call`,
    `<phone_id>_hangup`, etc. become visible via
    `notifications/tools/list_changed`.

    Recording defaults to on: every call on this phone is written to
    `/recordings/<phone_id>/call_<call_id>_<ts>.wav` (with a `.meta.json`
    sidecar alongside). Pass `recording_enabled=False` to suppress
    recording for this phone, or flip it at runtime via
    `update_phone(phone_id=..., recording_enabled=...)`.
    """
    try:
        result = _add_phone_impl(
            phone_id=phone_id,
            domain=domain, username=username, password=password,
            realm=realm, srtp=srtp, auto_answer=auto_answer,
            transport=transport, local_port=local_port,
            codecs=codecs, register=register,
            recording_enabled=recording_enabled,
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
) -> dict[str, Any]:
    """Mutate runtime-parameters of an existing phone.

    auto_answer — instantaneous.
    codecs — endpoint-wide priorities (affects all phones).
    recording_enabled — instantaneous; flips recording on/off on every
      currently active call of this phone. off→on starts a new WAV (with
      a new microsecond-unique filename); on→off closes the current WAV
      and writes its `.meta.json` sidecar. A call can cycle through
      on→off→on→... any number of times before DISCONNECTED.
    password / realm / srtp — force a fresh REGISTER cycle.
    """
    assert registry is not None and engine is not None and call_mgr is not None
    try:
        cfg = registry.get_config(phone_id)
        if cfg is None:
            return {"status": "error", "error": f"Phone {phone_id!r} not registered"}

        reregister_needed = False
        affected_call_ids: list[int] | None = None
        if auto_answer is not None:
            cfg.auto_answer = auto_answer
        if codecs is not None:
            engine.set_codecs(codecs)
            cfg.codecs = codecs
        if recording_enabled is not None:
            cfg.recording_enabled = recording_enabled
            affected_call_ids = call_mgr.set_recording_enabled(phone_id, recording_enabled)
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
            "recording_enabled": cfg.recording_enabled,
            **registry.get_registration_info(phone_id),
        }
        if affected_call_ids is not None:
            response["affected_call_ids"] = affected_call_ids
        return response
    except Exception as e:
        log.exception("update_phone failed")
        return {"status": "error", "error": str(e), "phone_id": phone_id}


_PHONE_FIELDS = {
    "phone_id", "domain", "username", "password", "realm", "srtp",
    "auto_answer", "transport", "local_port", "codecs", "register",
    "recording_enabled",
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
            f"Profile not found: {path}. Call get_phone_profile_example() "
            f"for a template, save it to ./config/phones.yaml on the host "
            f"(mounted read-only to /config in the container), then retry."
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
async def get_phone_profile_example() -> dict[str, Any]:
    """Return a YAML template for `load_phone_profile`.

    Use this when you don't know the phone-profile format — the returned
    `template` field is a ready-to-edit YAML. Save it as ./config/phones.yaml
    on the host, fill in your SIP credentials, then call `load_phone_profile`.

    Returns a dict with:
      - template: YAML string (copy to phones.yaml and edit)
      - save_to: Where to put the file (host path + container path)
      - next_step: The MCP tool to call after saving
      - example_file_in_container: Canonical path of the example on disk
        (accessible via the ./config bind mount)
    """
    return {
        "template": PHONE_PROFILE_TEMPLATE,
        "save_to": {
            "host_path": "./config/phones.yaml (in the pjsua_mcp repo)",
            "container_path": DEFAULT_PROFILE_PATH,
        },
        "next_step": f"mcp__pjsua__load_phone_profile(path='{DEFAULT_PROFILE_PATH}')",
        "example_file_in_container": EXAMPLE_PROFILE_PATH,
        "notes": [
            "phones.yaml is gitignored; phones.example.yaml is the tracked template.",
            "/config is mounted read-only — edit the file on the host, not in the container.",
            "load_phone_profile defaults to replace mode (drops existing phones + calls).",
            "Pass merge=True to keep phones not listed in the profile.",
        ],
    }


@mcp.tool()
async def load_phone_profile(
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
        log.exception("load_phone_profile: parse failed")
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
                    log.exception("load_phone_profile: drop %s failed", pid)
                    errors.append({"phone_id": pid, "error": f"drop failed: {e}"})

    # 3. Add phones from the profile. _add_phone_impl handles per-phone_id
    # replacement too, so merge mode still overwrites matching phone_ids.
    for spec in specs:
        try:
            res = _add_phone_impl(**spec)
            results.append(res)
        except Exception as e:
            log.exception("load_phone_profile[%s] failed", spec.get("phone_id"))
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
# Codecs (global, endpoint-wide)
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_codecs() -> dict[str, Any]:
    """List all endpoint codecs with their current priorities."""
    assert engine is not None
    try:
        return {"codecs": engine.get_codecs()}
    except Exception as e:
        log.exception("get_codecs failed")
        return {"status": "error", "error": str(e)}


@mcp.tool()
async def set_codecs(
    codecs: list[str],
    phone_id: str | None = None,
    call_id: int | None = None,
) -> dict[str, Any]:
    """Set endpoint-wide codec priorities (affects all phones).

    If `phone_id` + `call_id` are both provided, also send a re-INVITE on
    that call to renegotiate media using the new codec set.
    """
    assert engine is not None and call_mgr is not None
    try:
        if phone_id is not None and call_id is not None:
            info = call_mgr.reinvite_with_codecs(codecs, phone_id=phone_id, call_id=call_id)
        else:
            enabled = engine.set_codecs(codecs)
            info = {"codecs": enabled, "reinvite": False}
        return {"status": "ok", **info}
    except Exception as e:
        log.exception("set_codecs failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# SIP log (global with optional per-phone filter)
# ---------------------------------------------------------------------------
@mcp.tool()
async def get_sip_log(
    last_n: int | None = None,
    filter_text: str | None = None,
    phone_id: str | None = None,
) -> dict[str, Any]:
    """Retrieve pjsua SIP log entries. With `phone_id`, also filter by username substring."""
    assert engine is not None and registry is not None
    try:
        # Combine filter_text with phone-specific username match.
        extra_filter: str | None = None
        if phone_id is not None:
            cfg = registry.get_config(phone_id)
            if cfg and cfg.username:
                extra_filter = f"sip:{cfg.username}@"

        entries = engine.get_log_entries(last_n=None, filter_text=filter_text)
        if extra_filter:
            entries = [e for e in entries if extra_filter in e["msg"]]
        if last_n is not None:
            entries = entries[-last_n:]
        return {"entries": entries, "total_count": len(entries)}
    except Exception as e:
        log.exception("get_sip_log failed")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Packet capture (global; phone_id resolves to transport port for BPF filter)
# ---------------------------------------------------------------------------
@mcp.tool()
async def start_capture(
    phone_id: str | None = None,
    interface: str = "any",
    port: int | None = None,
) -> dict[str, Any]:
    """Start tcpdump.

    - No `phone_id`: host-wide capture → `/captures/capture_<ts>.pcap`.
    - With `phone_id`: BPF filters by that phone's UDP transport port and
      the pcap lands under `/captures/<phone_id>/`.
    - With `phone_id` + an active call on that phone: pcap filename matches
      the active recording's basename (e.g. `call_0_<ts>.pcap` next to
      `/recordings/<phone_id>/call_0_<ts>.wav`), so pcap and wav pair up
      without cross-referencing timestamps.
    """
    assert pcap_mgr is not None and engine is not None and registry is not None
    try:
        active_call_id: int | None = None
        if phone_id is not None:
            if port is None:
                cfg = registry.get_config(phone_id)
                if cfg is None or cfg.transport_id is None:
                    return {"status": "error", "error": f"Phone {phone_id!r} has no transport"}
                port = engine.get_transport_port(cfg.transport_id)
            if call_mgr is not None:
                active_call_id = call_mgr.get_active_call_id(phone_id)
        info = await pcap_mgr.start(
            interface=interface,
            port=port,
            phone_id=phone_id,
            call_id=active_call_id,
        )
        return {
            "status": "ok",
            "phone_id": phone_id,
            "call_id": active_call_id,
            "port": port,
            **info,
        }
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
    """Get info about a pcap file (defaults to most recent capture)."""
    assert pcap_mgr is not None
    try:
        return pcap_mgr.get_pcap_info(filename=filename)
    except Exception as e:
        log.exception("get_pcap failed")
        return {"status": "error", "error": str(e)}


_RECORDINGS_ROOT = Path("/recordings")


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
