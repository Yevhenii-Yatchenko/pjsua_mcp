"""SIP call management — outbound/inbound calls, DTMF, hold/unhold, recording."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pjsua2 as pj

from .sip_engine import SipEngine
from .account_manager import PhoneRegistry, DEFAULT_PHONE_ID

log = logging.getLogger(__name__)

RECORDINGS_DIR = Path("/recordings")
DEFAULT_MOH_FILE = Path("/app/audio/moh.wav")


class SipCall(pj.Call):
    """PJSUA2 Call subclass with state, media, recording, and playback callbacks."""

    def __init__(
        self,
        account: pj.Account,
        call_id: int = pj.PJSUA_INVALID_ID,
        phone_id: str = DEFAULT_PHONE_ID,
    ) -> None:
        super().__init__(account, call_id)
        self.phone_id = phone_id
        self._lock = threading.Lock()
        self._recorder: pj.AudioMediaRecorder | None = None
        self._recording_file: str | None = None
        self._player: pj.AudioMediaPlayer | None = None
        self._player_file: str | None = None
        self._aud_med: pj.AudioMedia | None = None  # cached for play/stop
        self.on_disconnected_cb: Any = None
        self._info: dict[str, Any] = {
            "phone_id": phone_id,
            "state": "NONE",
            "state_text": "",
            "remote_uri": "",
            "last_status": 0,
            "last_status_text": "",
            "duration": 0,
            "codec": "",
            "recording_file": None,
            "playing_file": None,
        }

    def onCallState(self, prm: pj.OnCallStateParam) -> None:
        ci = self.getInfo()
        with self._lock:
            self._info.update({
                "state": _call_state_name(ci.state),
                "state_text": ci.stateText,
                "remote_uri": ci.remoteUri,
                "last_status": ci.lastStatusCode,
                "last_status_text": ci.lastReason,
                "duration": ci.connectDuration.sec,
            })
        if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            self._stop_recording()
            self._stop_player()
            if self.on_disconnected_cb:
                self.on_disconnected_cb(self.get_cached_info())
        log.info(
            "[%s] Call %d state: %s (%s) remote=%s",
            self.phone_id, ci.id, ci.stateText, _call_state_name(ci.state), ci.remoteUri,
        )

    def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:
        ci = self.getInfo()
        for i, mi in enumerate(ci.media):
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                aud_med = self.getAudioMedia(i)
                self._aud_med = aud_med
                # Connect remote audio to playback device (for recording/monitoring)
                try:
                    ep = pj.Endpoint.instance()
                    aud_med.startTransmit(ep.audDevManager().getPlaybackDevMedia())
                except Exception:
                    log.exception("Error connecting playback for call %d", ci.id)

                # (Re)connect audio source → call.
                # After re-INVITE, aud_med is a NEW port — existing
                # player/capture connections are lost. Must reconnect.
                if self._player is not None:
                    try:
                        self._player.startTransmit(aud_med)
                    except Exception:
                        log.debug("Player reconnect to new aud_med failed")
                else:
                    self._start_default_moh()

                # Start/reconnect recording AFTER player setup.
                # player.startTransmit(aud_med) can disrupt existing
                # connections on the conference bridge, so recorder must
                # be connected last and reconnected on every media state change.
                self._ensure_recording(ci.id, aud_med)

                # Try to get codec info
                try:
                    si = self.getStreamInfo(i)
                    with self._lock:
                        self._info["codec"] = si.codecName
                except Exception:
                    pass
        log.info("[%s] Call %d media state updated", self.phone_id, ci.id)

    # --- Recording ---

    def _ensure_recording(self, call_id: int, aud_med: pj.AudioMedia) -> None:
        """Create recorder once, (re)connect it to aud_med every time.

        Must be called AFTER the player is set up — player.startTransmit()
        can disrupt existing conference bridge connections.
        """
        # Create recorder file once per call
        if self._recorder is None:
            try:
                RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                # Embed phone_id in filename for multi-phone disambiguation
                filename = f"call_{self.phone_id}_{call_id}_{timestamp}.wav"
                filepath = RECORDINGS_DIR / filename

                recorder = pj.AudioMediaRecorder()
                recorder.createRecorder(str(filepath))
                self._recorder = recorder
                self._recording_file = str(filepath)
                with self._lock:
                    self._info["recording_file"] = str(filepath)
                log.info("[%s] Created recorder for call %d: %s", self.phone_id, call_id, filepath)
            except Exception:
                log.exception("Failed to create recorder for call %d", call_id)
                return

        # (Re)connect: remote audio → recorder
        try:
            aud_med.startTransmit(self._recorder)
        except Exception:
            log.exception("Failed to connect recorder for call %d", call_id)

        # Also connect local audio (player/MOH) → recorder for full mix
        if self._player is not None:
            try:
                self._player.startTransmit(self._recorder)
            except Exception:
                log.exception("Failed to connect player to recorder for call %d", call_id)

    def _stop_recording(self) -> None:
        """Stop recording (called on DISCONNECTED)."""
        if self._recorder is not None:
            log.info("[%s] Stopping recording: %s", self.phone_id, self._recording_file)
            self._recorder = None

    # --- Audio playback ---

    def _start_default_moh(self) -> None:
        """Start playing default MOH into the call."""
        if DEFAULT_MOH_FILE.exists():
            self.play_file(str(DEFAULT_MOH_FILE), loop=True)
        else:
            log.warning("Default MOH file not found: %s", DEFAULT_MOH_FILE)

    def play_file(self, file_path: str, loop: bool = True) -> None:
        """Play a WAV file into the call audio stream.

        Stops any currently playing file first.
        """
        if self._aud_med is None:
            raise RuntimeError("Call has no active audio media")

        # Stop previous player
        self._stop_player()

        options = 0 if loop else pj.PJMEDIA_FILE_NO_LOOP
        player = pj.AudioMediaPlayer()
        player.createPlayer(file_path, options)
        player.startTransmit(self._aud_med)

        # Also feed player into recorder for full call mix
        if self._recorder is not None:
            try:
                player.startTransmit(self._recorder)
            except Exception:
                log.debug("Could not connect new player to recorder")

        self._player = player
        self._player_file = file_path
        with self._lock:
            self._info["playing_file"] = file_path
        log.info("[%s] Playing %s (loop=%s) into call", self.phone_id, file_path, loop)

    def stop_playback(self) -> None:
        """Stop audio playback and resume default MOH."""
        self._stop_player()
        # Resume default MOH
        self._start_default_moh()

    def _stop_player(self) -> None:
        """Stop the current audio player."""
        if self._player is not None:
            try:
                if self._aud_med is not None:
                    self._player.stopTransmit(self._aud_med)
            except Exception:
                pass
            self._player = None
            self._player_file = None
            with self._lock:
                self._info["playing_file"] = None

    @property
    def audio_media(self) -> pj.AudioMedia | None:
        return self._aud_med

    def get_cached_info(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._info)


def _call_state_name(state: int) -> str:
    names = {
        pj.PJSIP_INV_STATE_NULL: "NULL",
        pj.PJSIP_INV_STATE_CALLING: "CALLING",
        pj.PJSIP_INV_STATE_INCOMING: "INCOMING",
        pj.PJSIP_INV_STATE_EARLY: "EARLY",
        pj.PJSIP_INV_STATE_CONNECTING: "CONNECTING",
        pj.PJSIP_INV_STATE_CONFIRMED: "CONFIRMED",
        pj.PJSIP_INV_STATE_DISCONNECTED: "DISCONNECTED",
    }
    return names.get(state, f"UNKNOWN({state})")


class CallManager:
    """High-level call operations across multiple phones."""

    def __init__(self, engine: SipEngine, registry: PhoneRegistry) -> None:
        self._engine = engine
        self._registry = registry
        self._calls: dict[int, SipCall] = {}
        # Per-phone queues for incoming calls and pending auto-answers
        self._incoming_queue: dict[str, list[int]] = {}
        self._auto_answer_pending: dict[str, list[int]] = {}
        self._call_phone: dict[int, str] = {}
        self._lock = threading.Lock()
        self._call_history: deque[dict] = deque(maxlen=100)

        # Wire registry hooks so callbacks attach to every new phone
        registry.on_phone_added = self._on_phone_added
        registry.on_phone_dropped = self._on_phone_dropped

    # Legacy back-compat — AccountManager/PhoneRegistry alias.
    @property
    def _account_mgr(self) -> PhoneRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # Phone-registry hooks
    # ------------------------------------------------------------------
    def _on_phone_added(self, phone_id: str) -> None:
        """Attach the incoming-call callback for a newly added phone."""
        acc = self._registry.get_account(phone_id)
        if acc is None:
            return
        acc.on_incoming_call_cb = self._make_incoming_handler(phone_id)
        # Initialise per-phone queues lazily.
        with self._lock:
            self._incoming_queue.setdefault(phone_id, [])
            self._auto_answer_pending.setdefault(phone_id, [])

    def _on_phone_dropped(self, phone_id: str) -> None:
        """Clean call/queue state associated with a dropped phone."""
        with self._lock:
            self._incoming_queue.pop(phone_id, None)
            self._auto_answer_pending.pop(phone_id, None)
            stale_calls = [cid for cid, pid in self._call_phone.items() if pid == phone_id]
            for cid in stale_calls:
                self._call_phone.pop(cid, None)
                self._calls.pop(cid, None)

    def _make_incoming_handler(self, phone_id: str) -> Callable[[int], None]:
        """Factory — closes over phone_id so incoming calls route to the right queue."""

        def _handler(call_id: int) -> None:
            self._on_incoming_call(phone_id, call_id)

        return _handler

    def _on_incoming_call(self, phone_id: str, call_id: int) -> None:
        acc = self._registry.get_account(phone_id)
        if acc is None:
            log.warning("[%s] Incoming call %d for unknown phone", phone_id, call_id)
            return
        call = SipCall(acc, call_id, phone_id=phone_id)
        call.on_disconnected_cb = self._on_call_disconnected
        with self._lock:
            self._calls[call_id] = call
            self._call_phone[call_id] = phone_id
            self._incoming_queue.setdefault(phone_id, []).append(call_id)
            if self._registry.get_config(phone_id) and self._registry.get_config(phone_id).auto_answer:
                self._auto_answer_pending.setdefault(phone_id, []).append(call_id)
        log.info("[%s] Incoming call %d queued", phone_id, call_id)

    def process_auto_answers(self) -> None:
        """Process pending auto-answer calls for every phone.

        Called from the event poll loop — NOT from inside a pjsua callback.
        Answering inside onIncomingCall causes disconnects because the call
        state machine isn't ready yet.
        """
        with self._lock:
            pending: list[tuple[str, int]] = []
            for pid, queue in self._auto_answer_pending.items():
                while queue:
                    pending.append((pid, queue.pop(0)))
        for pid, call_id in pending:
            with self._lock:
                call = self._calls.get(call_id)
            if call is None:
                continue
            try:
                prm = pj.CallOpParam()
                prm.statusCode = 200
                call.answer(prm)
                log.info("[%s] Auto-answered call %d", pid, call_id)
            except Exception:
                log.exception("[%s] Failed to auto-answer call %d", pid, call_id)

    # ------------------------------------------------------------------
    # Phone resolution
    # ------------------------------------------------------------------
    def _resolve_phone(self, phone_id: str | None) -> str:
        """Resolve phone_id — explicit or fall back to 'default' if present."""
        if phone_id is not None:
            return phone_id
        if self._registry.has_phone(DEFAULT_PHONE_ID):
            return DEFAULT_PHONE_ID
        # Fallback: if there's exactly one phone, use it.
        ids = self._registry.list_phone_ids()
        if len(ids) == 1:
            return ids[0]
        raise RuntimeError("phone_id is required — multiple phones registered")

    def _ensure_incoming_handler(self, phone_id: str | None = None) -> None:
        """Ensure incoming-call callback is wired for the given phone (or all)."""
        ids = [self._resolve_phone(phone_id)] if phone_id else self._registry.list_phone_ids()
        for pid in ids:
            acc = self._registry.get_account(pid)
            if acc is not None and acc.on_incoming_call_cb is None:
                acc.on_incoming_call_cb = self._make_incoming_handler(pid)

    # ------------------------------------------------------------------
    # Outbound / inbound call lifecycle
    # ------------------------------------------------------------------
    def make_call(
        self,
        dest_uri: str,
        phone_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Place an outbound call from `phone_id`."""
        pid = self._resolve_phone(phone_id)
        self._ensure_incoming_handler(pid)
        acc = self._registry.require_account(pid)

        call = SipCall(acc, phone_id=pid)
        call.on_disconnected_cb = self._on_call_disconnected
        prm = pj.CallOpParam(True)  # True = use default call settings

        if headers:
            sip_headers = pj.SipHeaderVector()
            for name, value in headers.items():
                hdr = pj.SipHeader()
                hdr.hName = name
                hdr.hValue = value
                sip_headers.append(hdr)
            prm.txOption.headers = sip_headers

        call.makeCall(dest_uri, prm)
        ci = call.getInfo()
        call_id = ci.id

        with self._lock:
            self._calls[call_id] = call
            self._call_phone[call_id] = pid

        log.info("[%s] Outbound call %d to %s", pid, call_id, dest_uri)
        return {"phone_id": pid, "call_id": call_id, "state": _call_state_name(ci.state)}

    def answer_call(
        self,
        phone_id: str | None = None,
        call_id: int | None = None,
        status_code: int = 200,
    ) -> dict[str, Any]:
        """Answer an incoming call on the specified phone."""
        pid = self._resolve_phone(phone_id)
        self._ensure_incoming_handler(pid)
        call = self._get_call(phone_id=pid, call_id=call_id, from_incoming=True)
        prm = pj.CallOpParam()
        prm.statusCode = status_code
        call.answer(prm)
        ci = call.getInfo()
        return {"phone_id": pid, "call_id": ci.id, "state": _call_state_name(ci.state)}

    def reject_call(
        self,
        phone_id: str | None = None,
        call_id: int | None = None,
        status_code: int = 486,
    ) -> dict[str, Any]:
        pid = self._resolve_phone(phone_id)
        call = self._get_call(phone_id=pid, call_id=call_id, from_incoming=True)
        prm = pj.CallOpParam()
        prm.statusCode = status_code
        call.hangup(prm)
        return {"phone_id": pid, "call_id": call_id, "status_code": status_code}

    def blind_transfer(
        self,
        dest_uri: str,
        phone_id: str | None = None,
        call_id: int | None = None,
    ) -> dict[str, Any]:
        pid = self._resolve_phone(phone_id)
        call = self._get_call(phone_id=pid, call_id=call_id)
        prm = pj.CallOpParam()
        call.xfer(dest_uri, prm)
        return {"phone_id": pid, "call_id": call_id, "transfer_to": dest_uri}

    def attended_transfer(
        self,
        phone_id: str | None = None,
        call_id: int | None = None,
        dest_call_id: int | None = None,
    ) -> dict[str, Any]:
        """Attended transfer — both call legs must belong to the same phone."""
        pid = self._resolve_phone(phone_id)
        with self._lock:
            active_calls = [
                (cid, call) for cid, call in self._calls.items()
                if self._call_phone.get(cid) == pid and call.isActive()
            ]
        if len(active_calls) < 2:
            raise RuntimeError(f"[{pid}] Need at least 2 active calls for attended transfer")

        if call_id is not None and dest_call_id is not None:
            self._ensure_call_belongs_to(pid, call_id)
            self._ensure_call_belongs_to(pid, dest_call_id)
            src_call = self._get_call_by_id(call_id)
            dst_call = self._get_call_by_id(dest_call_id)
        else:
            src_call = active_calls[0][1]
            dst_call = active_calls[1][1]

        prm = pj.CallOpParam()
        src_call.xferReplaces(dst_call, prm)
        return {"phone_id": pid, "transferred": True}

    def hangup(
        self,
        phone_id: str | None = None,
        call_id: int | None = None,
    ) -> None:
        pid = self._resolve_phone(phone_id)
        call = self._get_call(phone_id=pid, call_id=call_id)
        prm = pj.CallOpParam()
        prm.statusCode = 603  # Decline
        try:
            call.hangup(prm)
        except pj.Error:
            log.debug("Call already disconnected")

    def hangup_all(self, phone_id: str | None = None) -> None:
        """Hang up all calls (optionally scoped to one phone)."""
        with self._lock:
            if phone_id is None:
                calls = list(self._calls.values())
            else:
                calls = [c for cid, c in self._calls.items() if self._call_phone.get(cid) == phone_id]
        for call in calls:
            try:
                if call.isActive():
                    prm = pj.CallOpParam()
                    prm.statusCode = 603
                    call.hangup(prm)
            except Exception:
                log.debug("Error hanging up call during cleanup")

    def get_call_info(
        self,
        phone_id: str | None = None,
        call_id: int | None = None,
    ) -> dict[str, Any]:
        """Get info about a call including RTP statistics."""
        pid = self._resolve_phone(phone_id)
        call = self._get_call(phone_id=pid, call_id=call_id)
        info = call.get_cached_info()
        info["phone_id"] = pid
        try:
            ci = call.getInfo()
            info["call_id"] = ci.id
            info["state"] = _call_state_name(ci.state)
            info["duration"] = ci.connectDuration.sec
            info["remote_contact"] = ci.remoteContact
            info["local_contact"] = ci.localContact
        except Exception:
            pass
        try:
            stat = call.getStreamStat(0)
            rtcp = stat.rtcp
            info["rtp"] = {
                "tx_packets": rtcp.txStat.pkt,
                "tx_bytes": rtcp.txStat.bytes,
                "rx_packets": rtcp.rxStat.pkt,
                "rx_bytes": rtcp.rxStat.bytes,
                "rx_loss": rtcp.rxStat.loss,
                "rx_dup": rtcp.rxStat.dup,
                "rx_reorder": rtcp.rxStat.reorder,
                "rx_discard": rtcp.rxStat.discard,
                "rx_jitter_usec": rtcp.rxStat.jitterUsec.last,
                "rtt_usec": rtcp.rttUsec.last,
            }
        except Exception:
            pass
        return info

    def send_dtmf(self, call_id: int, digits: str, phone_id: str | None = None) -> None:
        pid = self._resolve_phone(phone_id)
        self._ensure_call_belongs_to(pid, call_id)
        call = self._get_call_by_id(call_id)
        for digit in digits:
            try:
                call.dialDtmf(digit)
            except (pj.Error, AttributeError):
                dparam = pj.CallSendDtmfParam()
                dparam.digits = digit
                dparam.method = pj.PJSUA_DTMF_METHOD_SIP_INFO
                call.sendDtmf(dparam)

    def reinvite_with_codecs(
        self,
        codecs: list[str],
        phone_id: str | None = None,
        call_id: int | None = None,
    ) -> dict[str, Any]:
        """Change codecs on an active call via re-INVITE.

        Codec priorities are endpoint-wide; this sets them then re-INVITEs
        the target call.
        """
        pid = self._resolve_phone(phone_id)
        enabled = self._engine.set_codecs(codecs)
        call = self._get_call(phone_id=pid, call_id=call_id)
        prm = pj.CallOpParam()
        call.reinvite(prm)
        return {"phone_id": pid, "codecs": enabled, "reinvite": True}

    def play_audio(
        self,
        file_path: str,
        phone_id: str | None = None,
        call_id: int | None = None,
        loop: bool = False,
    ) -> dict[str, Any]:
        pid = self._resolve_phone(phone_id)
        call = self._get_call(phone_id=pid, call_id=call_id)
        call.play_file(file_path, loop=loop)
        return {"phone_id": pid, "call_id": call_id, "playing_file": file_path, "loop": loop}

    def stop_audio(
        self,
        phone_id: str | None = None,
        call_id: int | None = None,
    ) -> None:
        pid = self._resolve_phone(phone_id)
        call = self._get_call(phone_id=pid, call_id=call_id)
        call.stop_playback()

    def conference(
        self,
        call_ids: list[int],
        phone_id: str | None = None,
    ) -> dict[str, Any]:
        """Bridge multiple calls — all must belong to the same phone."""
        pid = self._resolve_phone(phone_id)
        for cid in call_ids:
            self._ensure_call_belongs_to(pid, cid)
        calls = [self._get_call_by_id(cid) for cid in call_ids]
        media_ports = [c.audio_media for c in calls if c.audio_media is not None]

        if len(media_ports) < 2:
            raise RuntimeError(f"[{pid}] Need at least 2 calls with active audio for conference")

        for i, port_a in enumerate(media_ports):
            for j, port_b in enumerate(media_ports):
                if i != j:
                    try:
                        port_a.startTransmit(port_b)
                    except Exception:
                        log.debug("Conference connect error: %d->%d", i, j)

        return {"phone_id": pid, "call_ids": call_ids, "participants": len(media_ports)}

    def hold(self, call_id: int, phone_id: str | None = None) -> None:
        pid = self._resolve_phone(phone_id)
        self._ensure_call_belongs_to(pid, call_id)
        call = self._get_call_by_id(call_id)
        prm = pj.CallOpParam()
        call.setHold(prm)

    def unhold(self, call_id: int, phone_id: str | None = None) -> None:
        pid = self._resolve_phone(phone_id)
        self._ensure_call_belongs_to(pid, call_id)
        call = self._get_call_by_id(call_id)
        prm = pj.CallOpParam()
        prm.flag = pj.PJSUA_CALL_UNHOLD
        call.reinvite(prm)

    def _on_call_disconnected(self, info: dict) -> None:
        """Record call to history and remove from active calls."""
        self._call_history.append({
            "phone_id": info.get("phone_id"),
            "remote_uri": info.get("remote_uri", ""),
            "duration": info.get("duration", 0),
            "last_status": info.get("last_status", 0),
            "last_status_text": info.get("last_status_text", ""),
            "codec": info.get("codec", ""),
            "recording_file": info.get("recording_file"),
            "timestamp": datetime.now().isoformat(),
        })
        with self._lock:
            stale = [cid for cid, c in self._calls.items()
                     if c.get_cached_info().get("state") == "DISCONNECTED"]
            for cid in stale:
                self._calls.pop(cid, None)
                self._call_phone.pop(cid, None)
            for pid, queue in self._incoming_queue.items():
                self._incoming_queue[pid] = [cid for cid in queue if cid in self._calls]

    def cleanup(self) -> None:
        """Clear all call state — called before re-registration."""
        with self._lock:
            self._calls.clear()
            self._call_phone.clear()
            for pid in list(self._incoming_queue):
                self._incoming_queue[pid].clear()
            for pid in list(self._auto_answer_pending):
                self._auto_answer_pending[pid].clear()

    def list_calls(self, phone_id: str | None = None) -> list[dict]:
        """List all tracked calls with basic status (optionally scoped to one phone)."""
        result = []
        with self._lock:
            items = list(self._calls.items())
        for cid, call in items:
            pid = self._call_phone.get(cid, call.phone_id)
            if phone_id is not None and pid != phone_id:
                continue
            info = call.get_cached_info()
            entry = {
                "phone_id": pid,
                "call_id": cid,
                "state": info.get("state", "UNKNOWN"),
                "remote_uri": info.get("remote_uri", ""),
                "duration": info.get("duration", 0),
                "codec": info.get("codec", ""),
            }
            try:
                ci = call.getInfo()
                entry["state"] = _call_state_name(ci.state)
                entry["call_id"] = ci.id
                entry["duration"] = ci.connectDuration.sec
            except Exception:
                pass
            result.append(entry)
        return result

    def get_active_calls(self, phone_id: str | None = None) -> list[dict]:
        """List only active (non-DISCONNECTED) calls with full info + RTP."""
        result = []
        for entry in self.list_calls(phone_id=phone_id):
            if entry["state"] not in ("DISCONNECTED", "NONE", "NULL"):
                try:
                    full = self.get_call_info(phone_id=entry["phone_id"], call_id=entry["call_id"])
                    result.append(full)
                except Exception:
                    result.append(entry)
        return result

    def get_call_history(
        self,
        phone_id: str | None = None,
        last_n: int | None = None,
    ) -> list[dict]:
        history = list(self._call_history)
        if phone_id is not None:
            history = [h for h in history if h.get("phone_id") == phone_id]
        if last_n:
            history = history[-last_n:]
        return history

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_call(
        self,
        phone_id: str | None = None,
        call_id: int | None = None,
        from_incoming: bool = False,
    ) -> SipCall:
        """Get a call by ID, or the first active/incoming call (scoped to phone_id)."""
        if call_id is not None:
            if phone_id is not None:
                self._ensure_call_belongs_to(phone_id, call_id)
            return self._get_call_by_id(call_id)

        pid = phone_id
        with self._lock:
            if from_incoming and pid is not None:
                queue = self._incoming_queue.get(pid, [])
                while queue:
                    cid = queue.pop(0)
                    if cid in self._calls:
                        return self._calls[cid]

            # Return the first active call that matches phone scope (if any)
            for cid, call in self._calls.items():
                if pid is not None and self._call_phone.get(cid) != pid:
                    continue
                try:
                    if call.isActive():
                        return call
                except Exception:
                    continue

        scope = f"phone {pid!r}" if pid else "any phone"
        raise RuntimeError(f"No active call found for {scope}")

    def _get_call_by_id(self, call_id: int) -> SipCall:
        with self._lock:
            if call_id in self._calls:
                return self._calls[call_id]
        raise RuntimeError(f"Call {call_id} not found")

    def _ensure_call_belongs_to(self, phone_id: str, call_id: int) -> None:
        """Raise if call_id does not belong to phone_id."""
        with self._lock:
            owner = self._call_phone.get(call_id)
        if owner is None:
            raise RuntimeError(f"Call {call_id} not found")
        if owner != phone_id:
            raise RuntimeError(
                f"call_id {call_id} belongs to phone_id {owner!r}, not {phone_id!r}"
            )
