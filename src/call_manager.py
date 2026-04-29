"""SIP call management — outbound/inbound calls, DTMF, hold/unhold, recording."""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import pjsua2 as pj

from .scenario_engine.event_bus import Event, emit_global
from .sip_engine import SipEngine
from .account_manager import PhoneRegistry, DEFAULT_PHONE_ID

if TYPE_CHECKING:
    from .pcap_manager import PcapManager

log = logging.getLogger(__name__)

DEFAULT_MOH_FILE = Path("/app/audio/moh.wav")
RECORDINGS_ROOT = Path("/recordings")


class SipCall(pj.Call):
    """PJSUA2 Call subclass with state, media, recording, and playback callbacks."""

    def __init__(
        self,
        account: pj.Account,
        call_id: int = pj.PJSUA_INVALID_ID,
        phone_id: str = DEFAULT_PHONE_ID,
        direction: str = "outbound",
        pcap_mgr: "PcapManager | None" = None,
        recording_enabled: bool = True,
        codecs: list[str] | None = None,
    ) -> None:
        super().__init__(account, call_id)
        self.phone_id = phone_id
        self._direction = direction
        self._pcap_mgr = pcap_mgr
        self._lock = threading.Lock()
        self._recorder: pj.AudioMediaRecorder | None = None
        self._recording_file: str | None = None
        self._recording_started_at: str | None = None
        self._recording_enabled = recording_enabled
        # Per-phone SDP filter — each outgoing offer/answer this call
        # produces is rewritten via `filter_audio_codecs` in
        # `onCallSdpCreated`. None or empty = no filter.
        self._codecs = list(codecs) if codecs else None
        # Toggle flipped to on while media isn't ready yet — start as soon
        # as onCallMediaState provides an aud_med.
        self._pending_start = False
        self._player: pj.AudioMediaPlayer | None = None
        self._player_file: str | None = None
        self._aud_med: pj.AudioMedia | None = None  # cached for play/stop
        # RTP ports captured while media is ACTIVE — getMedTransportInfo
        # is unreliable post-disconnect (media transport is torn down
        # before _stop_recording writes the meta sidecar).
        self._local_rtp_port: int | None = None
        self._remote_rtp_port: int | None = None
        self.on_disconnected_cb: Any = None
        # Fires exactly once, when onCallMediaState sees audio go ACTIVE for
        # the first time. CallManager wires this to its auto-capture counter
        # so the tcpdump opens on the first leg of a conference.
        self.on_first_active_cb: Any = None
        self._auto_capture_started = False
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
        state_name = _call_state_name(ci.state).lower()
        emit_global(
            Event(
                type=f"call.state.{state_name}",
                phone_id=self.phone_id,
                call_id=ci.id,
                data={
                    "state": state_name,
                    "remote_uri": ci.remoteUri,
                    "last_status": ci.lastStatusCode,
                    "last_status_text": ci.lastReason,
                    "direction": self._direction,
                },
            )
        )

    def onDtmfDigit(self, prm: pj.OnDtmfDigitParam) -> None:  # pragma: no cover - hardware event
        ci = self.getInfo()
        digit = str(getattr(prm, "digit", "")) or ""
        log.info("[%s] Call %d DTMF: %s", self.phone_id, ci.id, digit)
        emit_global(
            Event(
                type="dtmf.in",
                phone_id=self.phone_id,
                call_id=ci.id,
                data={"digit": digit, "method": "rfc2833"},
            )
        )

    def onCallSdpCreated(self, prm) -> None:
        """Rewrite outgoing SDP to the per-phone codec subset.

        Fires for every outgoing SDP (initial INVITE offer, 200 OK
        answer, re-INVITE in either role). The C++ bridge re-parses
        `prm.sdp.wholeSdp` after we return — see
        `pjsua2/endpoint.cpp::Endpoint::on_call_sdp_created`. We are
        called on the asyncio thread (single-threaded by design).

        No-op when `self._codecs` is None/empty — pjsua's endpoint
        priorities then govern, which after `enable_audio_codec_superset`
        is just "all audio codecs we know about".
        """
        from .sdp_rewriter import filter_audio_codecs

        if not self._codecs:
            return
        try:
            sdp_in = prm.sdp.wholeSdp
        except AttributeError:
            return  # defensive — pjsua should always provide .sdp.wholeSdp
        if not sdp_in:
            return

        sdp_out = filter_audio_codecs(
            sdp_in, self._codecs, preserve_dtmf=True,
        )
        if sdp_out != sdp_in:
            prm.sdp.wholeSdp = sdp_out
            log.debug(
                "[%s] SDP rewritten — codecs=%s, role=%s",
                self.phone_id, self._codecs,
                "answerer" if (
                    getattr(prm, "remSdp", None)
                    and getattr(prm.remSdp, "wholeSdp", "")
                ) else "offerer",
            )

    def onCallMediaState(self, prm: pj.OnCallMediaStateParam) -> None:
        ci = self.getInfo()
        for i, mi in enumerate(ci.media):
            if mi.type == pj.PJMEDIA_TYPE_AUDIO and mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE:
                aud_med = self.getAudioMedia(i)
                self._aud_med = aud_med
                # First active audio for this call → poke CallManager so it
                # can bump its active-calls-per-phone counter and (if the
                # phone has capture_enabled) queue an auto-capture start.
                # Flag guards against re-INVITE / media-refresh replays.
                if not self._auto_capture_started and self.on_first_active_cb:
                    self._auto_capture_started = True
                    try:
                        self.on_first_active_cb(self.phone_id, ci.id)
                    except Exception:
                        log.exception("on_first_active_cb failed for call %d", ci.id)
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
                self._reconnect_recorder(ci.id, aud_med)

                # Try to get codec info
                try:
                    si = self.getStreamInfo(i)
                    with self._lock:
                        self._info["codec"] = si.codecName
                except Exception:
                    pass

                # Snapshot RTP transport endpoints while media is alive —
                # they are unavailable in onCallState DISCONNECTED.
                self._snapshot_rtp_ports(i)
        log.info("[%s] Call %d media state updated", self.phone_id, ci.id)

    def _snapshot_rtp_ports(self, med_idx: int) -> None:
        """Capture (local, remote) RTP UDP ports for analyse_capture's
        per-phone summary. Best-effort: failures leave the previous
        snapshot in place so a later media state still wins.
        """
        try:
            mt = self.getMedTransportInfo(med_idx)
        except Exception:
            return

        def _port(name: object) -> int | None:
            try:
                return int(str(name).rsplit(":", 1)[-1])
            except (ValueError, AttributeError, IndexError):
                return None

        local = _port(getattr(mt, "localRtpName", None))
        remote = _port(getattr(mt, "srcRtpName", None))
        if local is not None:
            self._local_rtp_port = local
        if remote is not None:
            self._remote_rtp_port = remote

    # --- Recording ---
    #
    # State machine (guarded by self._recording_enabled):
    #   idle     → _recorder is None, _pending_start=False
    #   pending  → toggle asked for recording but media not yet active
    #   active   → _recorder is not None, _recording_file set
    #
    # _start_recording creates a fresh recorder with a microsecond-precision
    # filename, so off→on toggles during one call produce distinct WAVs.
    # _stop_recording writes the sidecar meta for the just-closed segment
    # and resets state back to idle — a later _start_recording writes a new
    # meta alongside the new WAV.

    def _start_recording(self, call_id: int, aud_med: pj.AudioMedia | None) -> None:
        """Start a new recording segment. Idempotent — returns early if already active."""
        if not self._recording_enabled:
            return
        if self._recorder is not None:
            return  # already recording
        if aud_med is None:
            # Media not active yet — flip pending and let _reconnect_recorder
            # finish the job when onCallMediaState fires.
            self._pending_start = True
            return

        try:
            recordings_path = RECORDINGS_ROOT / self.phone_id
            try:
                recordings_path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                log.info(
                    "[%s] Recording skipped: %s not writable (%s)",
                    self.phone_id, recordings_path, e,
                )
                return
            # Microsecond suffix so rapid off→on→off→on cycles never collide.
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"call_{call_id}_{timestamp}.wav"
            filepath = recordings_path / filename

            recorder = pj.AudioMediaRecorder()
            recorder.createRecorder(str(filepath))
            self._recorder = recorder
            self._recording_file = str(filepath)
            self._recording_started_at = datetime.now(timezone.utc).isoformat()
            self._pending_start = False
            with self._lock:
                self._info["recording_file"] = str(filepath)
            log.info("[%s] Created recorder for call %d: %s", self.phone_id, call_id, filepath)
        except Exception:
            log.exception("Failed to create recorder for call %d", call_id)
            return

        try:
            aud_med.startTransmit(self._recorder)
        except Exception:
            log.exception("Failed to connect remote audio to recorder for call %d", call_id)

        if self._player is not None:
            try:
                self._player.startTransmit(self._recorder)
            except Exception:
                log.exception("Failed to connect player to recorder for call %d", call_id)

    def _reconnect_recorder(self, call_id: int, aud_med: pj.AudioMedia) -> None:
        """Rewire recorder to a (re-)activated aud_med port.

        Called from onCallMediaState. If recording isn't enabled this is a
        no-op. If a toggle-on arrived before media was ready, this is where
        the deferred start finally happens.
        """
        if not self._recording_enabled:
            return
        if self._pending_start or self._recorder is None:
            self._start_recording(call_id, aud_med)
            return
        # Recorder exists — re-INVITE or media-state refresh hands us a new
        # aud_med port, so reconnect remote + local audio to the recorder.
        try:
            aud_med.startTransmit(self._recorder)
        except Exception:
            log.exception("Failed to reconnect recorder for call %d", call_id)
        if self._player is not None:
            try:
                self._player.startTransmit(self._recorder)
            except Exception:
                log.exception("Failed to reconnect player to recorder for call %d", call_id)

    def _stop_recording(self) -> None:
        """Close the current recording segment and write its meta sidecar.

        Idempotent — if no recorder is active, does nothing. Leaves the
        call in a state ready for another _start_recording if the toggle
        flips back on before DISCONNECTED.
        """
        if self._recorder is None:
            self._pending_start = False
            return
        log.info("[%s] Stopping recording: %s", self.phone_id, self._recording_file)
        self._write_meta_sidecar()
        self._recorder = None
        self._recording_file = None
        self._recording_started_at = None
        self._pending_start = False
        with self._lock:
            self._info["recording_file"] = None

    def _write_meta_sidecar(self) -> None:
        """Write `<recording>.meta.json` for the currently active segment."""
        if not self._recording_file:
            return
        try:
            rec_path = Path(self._recording_file)
            meta_path = rec_path.with_suffix(".meta.json")
            with self._lock:
                info = dict(self._info)
            try:
                ci = self.getInfo()
                call_id: int | None = ci.id
            except Exception:
                call_id = None
                stem = rec_path.stem  # "call_<id>_<date>_<time>_<us>"
                parts = stem.split("_")
                if len(parts) >= 2:
                    try:
                        call_id = int(parts[1])
                    except ValueError:
                        call_id = None
            pcap_path: str | None = None
            if self._pcap_mgr is not None:
                try:
                    pcap_path = self._pcap_mgr.current_pcap_path_for(self.phone_id)
                except Exception:
                    pcap_path = None
            # Take a final snapshot — onCallMediaState may not have fired
            # if media never went ACTIVE (e.g., call rejected before 200 OK).
            self._snapshot_rtp_ports(0)
            meta = {
                "phone_id": self.phone_id,
                "call_id": call_id,
                "direction": self._direction,
                "started_at": self._recording_started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "duration": info.get("duration", 0),
                "codec": info.get("codec", ""),
                "remote_uri": info.get("remote_uri", ""),
                "last_status": info.get("last_status", 0),
                "last_status_text": info.get("last_status_text", ""),
                "recording": str(rec_path),
                "pcap": pcap_path,
                "local_rtp_port": self._local_rtp_port,
                "remote_rtp_port": self._remote_rtp_port,
            }
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            log.info("[%s] Wrote recording meta: %s", self.phone_id, meta_path)
        except Exception:
            log.exception("Failed to write recording meta for %s", self._recording_file)


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

    def __init__(
        self,
        engine: SipEngine,
        registry: PhoneRegistry,
        pcap_mgr: "PcapManager | None" = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._pcap_mgr = pcap_mgr
        self._calls: dict[int, SipCall] = {}
        # Per-phone queues for incoming calls and pending auto-answers
        self._incoming_queue: dict[str, list[int]] = {}
        self._auto_answer_pending: dict[str, list[int]] = {}
        self._call_phone: dict[int, str] = {}
        self._lock = threading.Lock()
        self._call_history: deque[dict] = deque(maxlen=100)

        # Auto-capture: active-call counter per phone + pending queues that
        # process_auto_captures drains from the asyncio poll loop. Enqueueing
        # happens from pj callbacks (sync); actual start/stop runs async.
        self._active_calls_by_phone: dict[str, int] = {}
        self._auto_capture_pending_start: deque[tuple[str, int | None]] = deque()
        self._auto_capture_pending_stop: deque[str] = deque()

        # Maps SIP Call-ID header → ownership info. Used by get_sip_log to
        # resolve which phone a logged SIP message belongs to without falling
        # back to substring-match on phone username (which produces
        # cross-leg false positives — bob's `From: <sip:alice@>` looks like
        # alice's message under naive substring filtering).
        # Entries persist past disconnect so historical filter still works.
        self._sip_call_id_index: dict[str, dict[str, Any]] = {}

        # Wire registry hooks so callbacks attach to every new phone
        registry.on_phone_added = self._on_phone_added
        registry.on_phone_dropped = self._on_phone_dropped

    def _track_sip_call_id(
        self,
        sip_call_id: str,
        phone_id: str,
        pjsua_call_id: int,
        remote_uri: str,
    ) -> None:
        """Record (sip_call_id → owner) mapping. No-op for empty call_id."""
        if not sip_call_id:
            return
        with self._lock:
            self._sip_call_id_index[sip_call_id] = {
                "phone_id": phone_id,
                "pjsua_call_id": pjsua_call_id,
                "remote_uri": remote_uri,
            }

    def get_sip_call_id_index(self) -> dict[str, dict[str, Any]]:
        """Snapshot of (sip_call_id → owner-info) for log-filter resolution."""
        with self._lock:
            return {k: dict(v) for k, v in self._sip_call_id_index.items()}

    def get_active_call_id(self, phone_id: str) -> int | None:
        """Return the first active call_id for `phone_id`, or None.

        Used by `start_capture` to pair the pcap filename with the active
        call so recording and capture share one basename.
        """
        with self._lock:
            for cid, call in self._calls.items():
                if self._call_phone.get(cid) != phone_id:
                    continue
                try:
                    if call.isActive():
                        return cid
                except Exception:
                    continue
        return None

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
            self._active_calls_by_phone.pop(phone_id, None)
            stale_calls = [cid for cid, pid in self._call_phone.items() if pid == phone_id]
            for cid in stale_calls:
                self._call_phone.pop(cid, None)
                self._calls.pop(cid, None)
        # If an auto-capture is still alive for this phone, queue a stop
        # so the pcap is flushed cleanly before the registry forgets it.
        if self._pcap_mgr is not None and self._pcap_mgr.is_phone_capturing(phone_id):
            self._auto_capture_pending_stop.append(phone_id)

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
        cfg = self._registry.get_config(phone_id)
        recording_enabled = cfg.recording_enabled if cfg else True
        call = SipCall(
            acc, call_id,
            phone_id=phone_id,
            direction="inbound",
            pcap_mgr=self._pcap_mgr,
            recording_enabled=recording_enabled,
            codecs=cfg.codecs if cfg else None,
        )
        call.on_disconnected_cb = self._on_call_disconnected
        call.on_first_active_cb = self._register_call_active
        with self._lock:
            self._calls[call_id] = call
            self._call_phone[call_id] = phone_id
            self._incoming_queue.setdefault(phone_id, []).append(call_id)
            if cfg and cfg.auto_answer:
                self._auto_answer_pending.setdefault(phone_id, []).append(call_id)

        # Capture SIP Call-ID + remote URI right after creation so future
        # log queries with phone_id="<this>" can resolve ownership
        # structurally instead of falling back to substring match.
        try:
            ci = call.getInfo()
            self._track_sip_call_id(
                sip_call_id=getattr(ci, "callIdString", "") or "",
                phone_id=phone_id,
                pjsua_call_id=call_id,
                remote_uri=getattr(ci, "remoteUri", "") or "",
            )
        except Exception:
            log.debug("[%s] could not capture SIP Call-ID for call %d", phone_id, call_id)

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
    # Auto-capture lifecycle — counter + queues drained from poll loop
    # ------------------------------------------------------------------
    def _register_call_active(self, phone_id: str, call_id: int) -> None:
        """Called from pj thread on the first audio-ACTIVE callback per call.

        Increments the active-call counter for this phone; if this is the
        first active call AND the phone has capture_enabled, queue a
        start_for_phone to run in the asyncio poll loop.
        """
        cfg = self._registry.get_config(phone_id)
        with self._lock:
            new_count = self._active_calls_by_phone.get(phone_id, 0) + 1
            self._active_calls_by_phone[phone_id] = new_count
        is_first = new_count == 1
        if is_first and cfg is not None and cfg.capture_enabled:
            self._auto_capture_pending_start.append((phone_id, call_id))

    async def process_auto_captures(self) -> None:
        """Drain auto-capture start/stop queues from the asyncio poll loop.

        Counterpart to `process_auto_answers`. The pj callback thread
        enqueues tuples; this coroutine dequeues and calls the async
        pcap_mgr methods. Skips items where the desired state already
        holds (e.g. capture already running, phone dropped mid-flight).
        """
        if self._pcap_mgr is None:
            return
        while self._auto_capture_pending_start:
            try:
                phone_id, call_id = self._auto_capture_pending_start.popleft()
            except IndexError:
                break
            if self._pcap_mgr.is_phone_capturing(phone_id):
                continue
            try:
                await self._pcap_mgr.start_for_phone(phone_id, call_id)
            except Exception:
                log.exception("[%s] auto-capture start failed", phone_id)
        while self._auto_capture_pending_stop:
            try:
                phone_id = self._auto_capture_pending_stop.popleft()
            except IndexError:
                break
            if not self._pcap_mgr.is_phone_capturing(phone_id):
                continue
            try:
                await self._pcap_mgr.stop_for_phone(phone_id)
            except Exception:
                log.exception("[%s] auto-capture stop failed", phone_id)

    def set_capture_enabled(self, phone_id: str, enabled: bool) -> dict[str, Any]:
        """Toggle auto-capture for `phone_id`. Applies instantly to live calls.

        Off → on with active calls: queue a start (captures packets from
        "now" onward — earlier SIP/RTP of the call is NOT retroactively
        captured).
        On → off with a live tcpdump: queue a stop; the pcap is flushed
        and closed, and subsequent calls while the flag stays off will
        not open a new one.
        """
        cfg = self._registry.get_config(phone_id)
        if cfg is None:
            raise RuntimeError(f"Phone {phone_id!r} not found")
        cfg.capture_enabled = enabled
        if enabled:
            with self._lock:
                has_active = self._active_calls_by_phone.get(phone_id, 0) > 0
                active_cid = next(
                    (cid for cid, pid in self._call_phone.items() if pid == phone_id),
                    None,
                )
            if (
                has_active
                and self._pcap_mgr is not None
                and not self._pcap_mgr.is_phone_capturing(phone_id)
            ):
                self._auto_capture_pending_start.append((phone_id, active_cid))
        else:
            if self._pcap_mgr is not None and self._pcap_mgr.is_phone_capturing(phone_id):
                self._auto_capture_pending_stop.append(phone_id)
        return {"phone_id": phone_id, "capture_enabled": enabled}

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
        cfg = self._registry.get_config(pid)
        recording_enabled = cfg.recording_enabled if cfg else True

        call = SipCall(
            acc,
            phone_id=pid,
            direction="outbound",
            pcap_mgr=self._pcap_mgr,
            recording_enabled=recording_enabled,
            codecs=cfg.codecs if cfg else None,
        )
        call.on_disconnected_cb = self._on_call_disconnected
        call.on_first_active_cb = self._register_call_active
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

        self._track_sip_call_id(
            sip_call_id=getattr(ci, "callIdString", "") or "",
            phone_id=pid,
            pjsua_call_id=call_id,
            remote_uri=getattr(ci, "remoteUri", "") or dest_uri,
        )

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
        # `pj.CallOpParam()` keeps useDefaults=False, so Call::reinvite on
        # the C++ side copies our `prm.opt` into the call setting verbatim.
        # By default `opt` has audioCount=0/flag=0 — pjsua then emits an
        # `m=audio 0` "rejected media" SDP without the UNHOLD bit. Both
        # fields must be set explicitly:
        #   * `opt.audioCount = 1` keeps the audio stream alive in the
        #     re-INVITE (otherwise pjsua marks media as disabled);
        #   * `opt.flag = PJSUA_CALL_UNHOLD` is what flips SDP direction
        #     back to sendrecv and bumps the `o=` version.
        # Setting `prm.flag = ...` directly (without `opt.`) only writes a
        # stray Python proxy attribute that the C++ side never reads.
        prm = pj.CallOpParam()
        prm.opt.audioCount = 1
        prm.opt.flag = pj.PJSUA_CALL_UNHOLD
        call.reinvite(prm)

    def set_codecs_for_phone(
        self,
        phone_id: str,
        codecs: list[str] | None,
    ) -> list[int]:
        """Update SipCall._codecs for every active call on `phone_id` and
        re-INVITE the CONFIRMED ones so the live media stream swaps codec.

        Calls in non-CONFIRMED states (CALLING, EARLY) are updated in
        place without a re-INVITE — their first INVITE/200 OK already
        encodes the new filter via onCallSdpCreated.

        Calls on hold currently get re-INVITEd with sendrecv (variant A —
        symmetric with set_recording_enabled). To keep a hold while
        swapping codec, unhold first or skip update_phone(codecs=...)
        on this phone until after unhold.

        Returns the list of call_ids that received a re-INVITE.
        """
        with self._lock:
            calls = [
                (cid, c) for cid, c in self._calls.items()
                if self._call_phone.get(cid) == phone_id
            ]
        new = list(codecs) if codecs else None
        affected: list[int] = []
        for cid, call in calls:
            call._codecs = new
            try:
                ci = call.getInfo()
            except pj.Error:
                continue
            if ci.state != pj.PJSIP_INV_STATE_CONFIRMED:
                continue
            try:
                call.reinvite(pj.CallOpParam(True))
                affected.append(cid)
            except pj.Error as e:
                log.warning(
                    "[%s] codec re-INVITE failed for call %d: %s",
                    phone_id, cid, e,
                )
        return affected

    def set_recording_enabled(self, phone_id: str, enabled: bool) -> list[int]:
        """Flip recording on/off for every active call on `phone_id`.

        On (off→on): starts a fresh recorder with a new microsecond-unique
        filename for each active call — or marks the call pending if media
        isn't active yet (then onCallMediaState picks it up).

        Off (on→off): closes the active recorder and writes its meta sidecar.
        A subsequent on→off→on produces a second distinct WAV + meta pair.

        Returns the list of call_ids that were touched.
        """
        with self._lock:
            calls = [
                (cid, c) for cid, c in self._calls.items()
                if self._call_phone.get(cid) == phone_id
            ]
        affected: list[int] = []
        for cid, call in calls:
            call._recording_enabled = enabled
            if enabled:
                if call._aud_med is not None:
                    call._start_recording(cid, call._aud_med)
                else:
                    call._pending_start = True
            else:
                call._stop_recording()
            affected.append(cid)
        return affected

    def _on_call_disconnected(self, info: dict) -> None:
        """Record call to history and remove from active calls.

        Also runs the auto-capture counter: decrements the per-phone
        active-call count and, if this was the last active call on a
        phone with a running auto-capture, queues a stop for
        `process_auto_captures` to drain.
        """
        phone_id = info.get("phone_id")
        self._call_history.append({
            "phone_id": phone_id,
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

            is_last = False
            if phone_id is not None and phone_id in self._active_calls_by_phone:
                count = self._active_calls_by_phone[phone_id] - 1
                if count <= 0:
                    self._active_calls_by_phone.pop(phone_id, None)
                    is_last = True
                else:
                    self._active_calls_by_phone[phone_id] = count
        if (
            is_last
            and phone_id is not None
            and self._pcap_mgr is not None
            and self._pcap_mgr.is_phone_capturing(phone_id)
        ):
            self._auto_capture_pending_stop.append(phone_id)

    def cleanup(self) -> None:
        """Clear all call state — called before re-registration."""
        with self._lock:
            self._calls.clear()
            self._call_phone.clear()
            self._active_calls_by_phone.clear()
            for pid in list(self._incoming_queue):
                self._incoming_queue[pid].clear()
            for pid in list(self._auto_answer_pending):
                self._auto_answer_pending[pid].clear()
        self._auto_capture_pending_start.clear()
        self._auto_capture_pending_stop.clear()

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
