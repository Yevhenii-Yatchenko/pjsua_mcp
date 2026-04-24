"""Action executor — dispatches actions from hook `then:` blocks to pjsua managers.

Actions can be specified in any of these equivalent forms:
  - "answer"                         → ("answer", {})
  - {answer: {}}                     → ("answer", {})
  - {answer: {code: 200}}            → ("answer", {"code": 200})
  - {send_dtmf: "1234"}              → ("send_dtmf", {"value": "1234"})
  - {wait: "500ms"}                  → ("wait", {"value": "500ms"})
  - {action: send_dtmf, digits: "1"} → ("send_dtmf", {"digits": "1"})

Default phone_id / call_id are inherited from hook.on_phone and event.call_id.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from src.scenario_engine.event_bus import Event, EventBus

if TYPE_CHECKING:
    from src.account_manager import PhoneRegistry
    from src.call_manager import CallManager
    from src.scenario_engine.hook_runtime import Hook
    from src.scenario_engine.timeline import TimelineRecorder
    from src.sip_engine import SipEngine


class ActionError(Exception):
    """Raised when an action fails to execute."""


_MS_RX = re.compile(r"^(\d+)\s*ms$")
_S_RX = re.compile(r"^(\d+(?:\.\d+)?)\s*s$")


def _parse_ms(value: Any) -> int:
    """Parse a duration: int/float (ms), '500ms', '2s', '1.5s'."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        m = _MS_RX.match(value)
        if m:
            return int(m.group(1))
        s = _S_RX.match(value)
        if s:
            return int(float(s.group(1)) * 1000)
        try:
            return int(value)
        except ValueError as e:
            raise ActionError(f"cannot parse duration: {value!r}") from e
    raise ActionError(f"unsupported duration value: {value!r}")


def normalize_action(spec: Any) -> tuple[str, dict[str, Any]]:
    """Canonicalise an action spec to (name, args_dict)."""
    if isinstance(spec, str):
        return spec, {}
    if isinstance(spec, dict):
        if "action" in spec:
            name = spec["action"]
            args = {k: v for k, v in spec.items() if k != "action"}
            return str(name), args
        if len(spec) == 1:
            name, val = next(iter(spec.items()))
            if val is None:
                return str(name), {}
            if isinstance(val, dict):
                return str(name), dict(val)
            return str(name), {"value": val}
        raise ActionError(f"ambiguous action spec (no `action:` key): {spec!r}")
    raise ActionError(f"unsupported action spec type: {type(spec).__name__}")


class ActionExecutor:
    def __init__(
        self,
        call_manager: "CallManager",
        registry: "PhoneRegistry",
        bus: EventBus,
        recorder: "TimelineRecorder",
        loop: asyncio.AbstractEventLoop,
        engine: "SipEngine | None" = None,
    ) -> None:
        self._cm = call_manager
        self._registry = registry
        self._bus = bus
        self._rec = recorder
        self._loop = loop
        self._engine = engine
        self._dispatch = {
            # Call control
            "answer": self._a_answer,
            "hangup": self._a_hangup,
            "hangup_all": self._a_hangup_all,
            "reject": self._a_reject,
            "hold": self._a_hold,
            "unhold": self._a_unhold,
            "send_dtmf": self._a_send_dtmf,
            "blind_transfer": self._a_blind_transfer,
            "attended_transfer": self._a_attended_transfer,
            "conference": self._a_conference,
            "make_call": self._a_make_call,
            # Media
            "play_audio": self._a_play_audio,
            "stop_audio": self._a_stop_audio,
            # Messaging
            "send_message": self._a_send_message,
            # Codec
            "set_codecs": self._a_set_codecs,
            # Flow control / meta
            "wait": self._a_wait,
            "wait_until": self._a_wait_until,
            "emit": self._a_emit,
            "checkpoint": self._a_checkpoint,
            "log": self._a_log,
        }

    async def execute(
        self,
        actions: list[Any],
        hook: "Hook",
        event: Event,
    ) -> None:
        for spec in actions:
            try:
                name, args = normalize_action(spec)
            except ActionError as e:
                self._rec.record_meta("action.error", {"error": str(e), "spec": spec})
                raise
            # Inherit defaults from hook+event
            if "phone_id" not in args:
                args["phone_id"] = hook.on_phone or event.phone_id
            if "call_id" not in args and event.call_id is not None:
                args["call_id"] = event.call_id
            handler = self._dispatch.get(name)
            if handler is None:
                self._rec.record_meta("action.unknown", {"name": name, "args": args})
                raise ActionError(f"unknown action: {name}")
            try:
                await handler(args, hook, event)
            except Exception as exc:  # noqa: BLE001
                self._rec.record_meta(
                    "action.failed",
                    {"name": name, "args": args, "error": repr(exc)},
                )
                raise

    # ----- individual action handlers -----

    async def _a_answer(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args.get("call_id")
        code = int(args.get("code", 200))
        self._rec.record_action(
            "answer", pid, cid, {"status_code": code}, hook.hook_id, hook.pattern_name
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.answer_call(phone_id=pid, call_id=cid, status_code=code),
        )

    async def _a_hangup(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args.get("call_id")
        self._rec.record_action("hangup", pid, cid, {}, hook.hook_id, hook.pattern_name)
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.hangup(phone_id=pid, call_id=cid),
        )

    async def _a_hangup_all(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        """Teardown: hang up every active call. Scoped to a phone if phone_id
        is set; global otherwise."""
        pid = args.get("phone_id")   # may be None → global hangup_all
        self._rec.record_action(
            "hangup_all", pid, None, {}, hook.hook_id, hook.pattern_name
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.hangup_all(phone_id=pid),
        )

    async def _a_reject(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args.get("call_id")
        code = int(args.get("code", args.get("value", 486)))
        self._rec.record_action(
            "reject", pid, cid, {"status_code": code}, hook.hook_id, hook.pattern_name
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.reject_call(phone_id=pid, call_id=cid, status_code=code),
        )

    async def _a_hold(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args["call_id"]
        self._rec.record_action("hold", pid, cid, {}, hook.hook_id, hook.pattern_name)
        await self._loop.run_in_executor(None, lambda: self._cm.hold(call_id=cid, phone_id=pid))

    async def _a_unhold(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args["call_id"]
        self._rec.record_action("unhold", pid, cid, {}, hook.hook_id, hook.pattern_name)
        await self._loop.run_in_executor(None, lambda: self._cm.unhold(call_id=cid, phone_id=pid))

    async def _a_send_dtmf(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args.get("call_id")
        digits = args.get("digits") or args.get("value")
        if digits is None:
            raise ActionError("send_dtmf requires `digits`")
        self._rec.record_action(
            "send_dtmf", pid, cid, {"digits": str(digits)}, hook.hook_id, hook.pattern_name
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.send_dtmf(call_id=cid, digits=str(digits), phone_id=pid),
        )

    async def _a_blind_transfer(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args.get("call_id")
        to = args.get("to") or args.get("dest_uri") or args.get("value")
        if not to:
            raise ActionError("blind_transfer requires `to` (or dest_uri)")
        self._rec.record_action(
            "blind_transfer", pid, cid, {"to": to}, hook.hook_id, hook.pattern_name
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.blind_transfer(dest_uri=to, phone_id=pid, call_id=cid),
        )

    async def _a_attended_transfer(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        """REFER/Replaces. Both call_id and dest_call_id must belong to the same phone."""
        pid = args["phone_id"]
        cid = args.get("call_id")
        dest_cid = args.get("dest_call_id") or args.get("target_call_id")
        self._rec.record_action(
            "attended_transfer", pid, cid,
            {"dest_call_id": dest_cid}, hook.hook_id, hook.pattern_name,
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.attended_transfer(
                phone_id=pid, call_id=cid, dest_call_id=dest_cid
            ),
        )

    async def _a_conference(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        """Bridge multiple calls via the local pjsua conference port.

        `call_ids` can be:
          - list of ints (explicit)
          - "auto" (or omitted) → use all currently-active calls on the phone
        """
        pid = args["phone_id"]
        call_ids = args.get("call_ids") or args.get("value")
        if call_ids in (None, "auto", ""):
            active = await self._loop.run_in_executor(
                None, lambda: self._cm.get_active_calls(phone_id=pid)
            )
            cids = [int(c["call_id"]) for c in active]
            if len(cids) < 2:
                raise ActionError(
                    f"conference auto: need ≥2 active calls on {pid}, got {len(cids)}"
                )
        elif isinstance(call_ids, (list, tuple)):
            cids = [int(c) for c in call_ids]
        else:
            cids = [int(call_ids)]
        self._rec.record_action(
            "conference", pid, None,
            {"call_ids": cids}, hook.hook_id, hook.pattern_name,
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.conference(call_ids=cids, phone_id=pid),
        )

    async def _a_make_call(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args.get("phone_id") or args.get("from_phone")
        if not pid:
            raise ActionError("make_call requires phone_id (or from_phone)")
        to = args.get("to") or args.get("dest_uri") or args.get("value")
        if not to:
            raise ActionError("make_call requires `to` (or dest_uri)")
        headers = args.get("headers") or None
        self._rec.record_action(
            "make_call", pid, None, {"dest_uri": to}, hook.hook_id, hook.pattern_name
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.make_call(dest_uri=to, phone_id=pid, headers=headers),
        )

    async def _a_play_audio(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args.get("call_id")
        path = args.get("file") or args.get("path") or args.get("file_path") or args.get("value")
        if not path:
            raise ActionError("play_audio requires `file` (or file_path/path)")
        loop = bool(args.get("loop", False))
        self._rec.record_action(
            "play_audio", pid, cid,
            {"file": str(path), "loop": loop}, hook.hook_id, hook.pattern_name,
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.play_audio(file_path=str(path), phone_id=pid, call_id=cid, loop=loop),
        )

    async def _a_stop_audio(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        pid = args["phone_id"]
        cid = args.get("call_id")
        self._rec.record_action(
            "stop_audio", pid, cid, {}, hook.hook_id, hook.pattern_name
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._cm.stop_audio(phone_id=pid, call_id=cid),
        )

    async def _a_send_message(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        """Out-of-dialog SIP MESSAGE (page-mode IM)."""
        pid = args["phone_id"]
        to = args.get("to") or args.get("dest_uri")
        body = args.get("body") or args.get("value")
        if not to:
            raise ActionError("send_message requires `to` (or dest_uri)")
        if body is None:
            raise ActionError("send_message requires `body` (or value)")
        ct = args.get("content_type", "text/plain")
        self._rec.record_action(
            "send_message", pid, None,
            {"to": to, "body": body, "content_type": ct},
            hook.hook_id, hook.pattern_name,
        )
        await self._loop.run_in_executor(
            None,
            lambda: self._registry.send_message(
                dest_uri=to, body=body, phone_id=pid, content_type=ct
            ),
        )

    async def _a_set_codecs(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        """Change endpoint codec priorities; optionally re-INVITE a call."""
        if self._engine is None:
            raise ActionError("set_codecs: SipEngine not wired into ActionExecutor")
        codecs = args.get("codecs") or args.get("value")
        if not codecs:
            raise ActionError("set_codecs requires `codecs`")
        if isinstance(codecs, str):
            codecs = [codecs]
        codecs = [str(c) for c in codecs]
        pid = args.get("phone_id")
        cid = args.get("call_id")
        self._rec.record_action(
            "set_codecs", pid, cid,
            {"codecs": codecs}, hook.hook_id, hook.pattern_name,
        )
        # 1) Endpoint-wide priority change.
        await self._loop.run_in_executor(None, lambda: self._engine.set_codecs(codecs))
        # 2) Optionally re-INVITE a specific call to renegotiate with new SDP.
        if pid and cid is not None:
            await self._loop.run_in_executor(
                None, lambda: self._cm.unhold(call_id=int(cid), phone_id=pid)
            )

    async def _a_wait(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        raw = args.get("ms") if "ms" in args else args.get("value")
        ms = _parse_ms(raw if raw is not None else 0)
        self._rec.record_action(
            "wait", args.get("phone_id"), args.get("call_id"),
            {"ms": ms}, hook.hook_id, hook.pattern_name,
        )
        await asyncio.sleep(ms / 1000.0)

    async def _a_wait_until(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        event_type = args.get("event") or args.get("value")
        if not event_type:
            raise ActionError("wait_until requires `event` (or value)")
        timeout_ms = _parse_ms(args.get("timeout_ms", 10000))
        self._rec.record_action(
            "wait_until", args.get("phone_id"), args.get("call_id"),
            {"event": event_type, "timeout_ms": timeout_ms},
            hook.hook_id, hook.pattern_name,
        )
        pid = args.get("phone_id")
        def predicate(e: Event) -> bool:
            return pid is None or e.phone_id == pid or e.phone_id is None
        await self._bus.wait_for(event_type, predicate=predicate, timeout=timeout_ms / 1000.0)

    async def _a_emit(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        name = args.get("name") or args.get("value")
        if not name:
            raise ActionError("emit requires `name` (or value)")
        data = dict(args.get("data") or {})
        self._bus.emit(
            Event(type=f"user.{name}", phone_id=args.get("phone_id"), data=data)
        )

    async def _a_checkpoint(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        label = args.get("label") or args.get("value") or ""
        self._rec.record_meta("checkpoint", {"label": str(label), "hook_id": hook.hook_id})

    async def _a_log(self, args: dict[str, Any], hook: "Hook", ev: Event) -> None:
        msg = args.get("message") or args.get("value") or ""
        self._rec.record_meta("log", {"message": str(msg), "hook_id": hook.hook_id})
