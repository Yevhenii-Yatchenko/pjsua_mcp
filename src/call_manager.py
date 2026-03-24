"""SIP call management — outbound/inbound calls, DTMF, hold/unhold, recording."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pjsua2 as pj

from .sip_engine import SipEngine
from .account_manager import AccountManager

log = logging.getLogger(__name__)

RECORDINGS_DIR = Path("/recordings")
DEFAULT_MOH_FILE = Path("/app/audio/moh.wav")


class SipCall(pj.Call):
    """PJSUA2 Call subclass with state, media, recording, and playback callbacks."""

    def __init__(self, account: pj.Account, call_id: int = pj.PJSUA_INVALID_ID) -> None:
        super().__init__(account, call_id)
        self._lock = threading.Lock()
        self._recorder: pj.AudioMediaRecorder | None = None
        self._recording_file: str | None = None
        self._player: pj.AudioMediaPlayer | None = None
        self._player_file: str | None = None
        self._aud_med: pj.AudioMedia | None = None  # cached for play/stop
        self._info: dict[str, Any] = {
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
        log.info(
            "Call %d state: %s (%s) remote=%s",
            ci.id, ci.stateText, _call_state_name(ci.state), ci.remoteUri,
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

                # Start default MOH FIRST (player → call audio)
                if self._player is None:
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
        log.info("Call %d media state updated", ci.id)

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
                filename = f"call_{call_id}_{timestamp}.wav"
                filepath = RECORDINGS_DIR / filename

                recorder = pj.AudioMediaRecorder()
                recorder.createRecorder(str(filepath))
                self._recorder = recorder
                self._recording_file = str(filepath)
                with self._lock:
                    self._info["recording_file"] = str(filepath)
                log.info("Created recorder for call %d: %s", call_id, filepath)
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
            log.info("Stopping recording: %s", self._recording_file)
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
        log.info("Playing %s (loop=%s) into call", file_path, loop)

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
    """High-level call operations."""

    def __init__(self, engine: SipEngine, account_mgr: AccountManager) -> None:
        self._engine = engine
        self._account_mgr = account_mgr
        self._calls: dict[int, SipCall] = {}
        self._incoming_queue: list[int] = []
        self._auto_answer_pending: list[int] = []
        self._lock = threading.Lock()

        # Wire up incoming call callback
        # This will be set when account is created
        self._setup_incoming_handler()

    def _setup_incoming_handler(self) -> None:
        """Set up the incoming call callback on the account."""
        acc = self._account_mgr.account
        if acc:
            acc.on_incoming_call_cb = self._on_incoming_call

    def _on_incoming_call(self, call_id: int) -> None:
        """Handle incoming call from account callback."""
        acc = self._account_mgr.account
        if not acc:
            return
        call = SipCall(acc, call_id)
        with self._lock:
            self._calls[call_id] = call
            self._incoming_queue.append(call_id)
        log.info("Incoming call %d queued", call_id)

        if self._account_mgr.auto_answer:
            self._auto_answer_pending.append(call_id)

    def process_auto_answers(self) -> None:
        """Process pending auto-answer calls.

        Called from the event poll loop — NOT from inside a pjsua callback.
        Answering inside onIncomingCall causes disconnects because the call
        state machine isn't ready yet.
        """
        while self._auto_answer_pending:
            call_id = self._auto_answer_pending.pop(0)
            with self._lock:
                call = self._calls.get(call_id)
            if call is None:
                continue
            try:
                prm = pj.CallOpParam()
                prm.statusCode = 200
                call.answer(prm)
                log.info("Auto-answered call %d", call_id)
            except Exception:
                log.exception("Failed to auto-answer call %d", call_id)

    def _ensure_incoming_handler(self) -> None:
        """Ensure incoming call handler is connected to current account."""
        acc = self._account_mgr.account
        if acc and acc.on_incoming_call_cb is None:
            acc.on_incoming_call_cb = self._on_incoming_call

    def make_call(
        self,
        dest_uri: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Place an outbound call."""
        self._ensure_incoming_handler()
        acc = self._account_mgr.account
        if not acc or not acc.isValid():
            raise RuntimeError("No valid account — register first")

        call = SipCall(acc)
        prm = pj.CallOpParam(True)  # True = use default call settings

        # Add custom headers
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

        log.info("Outbound call %d to %s", call_id, dest_uri)
        return {"call_id": call_id, "state": _call_state_name(ci.state)}

    def answer_call(
        self,
        call_id: int | None = None,
        status_code: int = 200,
    ) -> dict[str, Any]:
        """Answer an incoming call."""
        self._ensure_incoming_handler()
        call = self._get_call(call_id, from_incoming=True)
        prm = pj.CallOpParam()
        prm.statusCode = status_code
        call.answer(prm)
        ci = call.getInfo()
        return {"call_id": ci.id, "state": _call_state_name(ci.state)}

    def reject_call(self, call_id: int | None = None, status_code: int = 486) -> dict[str, Any]:
        """Reject an incoming call with a SIP error code."""
        call = self._get_call(call_id, from_incoming=True)
        prm = pj.CallOpParam()
        prm.statusCode = status_code
        call.hangup(prm)
        return {"call_id": call_id, "status_code": status_code}

    def hangup(self, call_id: int | None = None) -> None:
        """Hang up a call."""
        call = self._get_call(call_id)
        prm = pj.CallOpParam()
        prm.statusCode = 603  # Decline
        try:
            call.hangup(prm)
        except pj.Error:
            log.debug("Call already disconnected")

    def hangup_all(self) -> None:
        """Hang up all active calls (cleanup)."""
        with self._lock:
            calls = list(self._calls.values())
        for call in calls:
            try:
                if call.isActive():
                    prm = pj.CallOpParam()
                    prm.statusCode = 603
                    call.hangup(prm)
            except Exception:
                log.debug("Error hanging up call during cleanup")

    def get_call_info(self, call_id: int | None = None) -> dict[str, Any]:
        """Get info about a call."""
        call = self._get_call(call_id)
        info = call.get_cached_info()
        # Also try to get fresh info
        try:
            ci = call.getInfo()
            info["call_id"] = ci.id
            info["state"] = _call_state_name(ci.state)
            info["duration"] = ci.connectDuration.sec
        except Exception:
            pass
        return info

    def send_dtmf(self, call_id: int, digits: str) -> None:
        """Send DTMF digits on a call."""
        call = self._get_call_by_id(call_id)
        prm = pj.CallOpParam()
        for digit in digits:
            try:
                call.dialDtmf(digit)
            except (pj.Error, AttributeError):
                # Fallback to INFO method
                dparam = pj.CallSendDtmfParam()
                dparam.digits = digit
                dparam.method = pj.PJSUA_DTMF_METHOD_SIP_INFO
                call.sendDtmf(dparam)

    def play_audio(self, file_path: str, call_id: int | None = None, loop: bool = False) -> dict[str, Any]:
        """Play a WAV file into a call's audio stream."""
        call = self._get_call(call_id)
        call.play_file(file_path, loop=loop)
        return {"call_id": call_id, "playing_file": file_path, "loop": loop}

    def stop_audio(self, call_id: int | None = None) -> None:
        """Stop audio playback on a call (resumes default MOH)."""
        call = self._get_call(call_id)
        call.stop_playback()

    def hold(self, call_id: int) -> None:
        """Put a call on hold."""
        call = self._get_call_by_id(call_id)
        prm = pj.CallOpParam()
        call.setHold(prm)

    def unhold(self, call_id: int) -> None:
        """Resume a held call."""
        call = self._get_call_by_id(call_id)
        prm = pj.CallOpParam()
        prm.flag = pj.PJSUA_CALL_UNHOLD
        call.reinvite(prm)

    def _get_call(self, call_id: int | None = None, from_incoming: bool = False) -> SipCall:
        """Get a call by ID, or the current active/incoming call."""
        if call_id is not None:
            return self._get_call_by_id(call_id)

        with self._lock:
            if from_incoming and self._incoming_queue:
                cid = self._incoming_queue.pop(0)
                if cid in self._calls:
                    return self._calls[cid]

            # Return any active call
            for call in self._calls.values():
                try:
                    if call.isActive():
                        return call
                except Exception:
                    continue

        raise RuntimeError("No active call found")

    def _get_call_by_id(self, call_id: int) -> SipCall:
        with self._lock:
            if call_id in self._calls:
                return self._calls[call_id]
        raise RuntimeError(f"Call {call_id} not found")
