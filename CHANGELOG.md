# Changelog

## [Unreleased]

### Added
- MCP tool `analyze_capture(phone_id, call_id=None)` — parses a phone's
  pcap into structured RTP/RTCP flow counts, classifies packets per
  RFC 3550 (V=2, RTCP PT 200..206 → rtcp_flows; everything else with
  V=2 → rtp_flows), and surfaces a per-phone summary
  (`phone_rtp_codecs_seen`, `non_phone_codecs_on_phone_port`) when
  the call's recording sidecar contains `local_rtp_port`. Supports
  libpcap linktypes 0/1/12/113/276 (DLT_NULL, EN10MB, RAW, LINUX_SLL,
  LINUX_SLL2). Replaces the inline 70-line `python3 - <<PY` blocks
  in plan-NN scripts.
- `src/pcap_analyzer.py` — pure-Python pcap walker built on `dpkt`
  (already a runtime dep). 19 unit tests in `tests/test_pcap_analyzer.py`
  cover linktypes, RTP/RTCP separation, marker-bit handling, and
  per-phone summary computation against synthesised + plan-01 real
  fixtures (`tests/fixtures/plan_01_alice.pcap`, `plan_01_bob.pcap`).
- Recording sidecar `.meta.json` now stores `local_rtp_port` and
  `remote_rtp_port`, snapshotted in `onCallMediaState` while media is
  ACTIVE (post-disconnect read of `getMedTransportInfo` is unreliable).

### Fixed
- `unhold(call_id)` was setting `prm.flag = PJSUA_CALL_UNHOLD` —
  a no-op, since `flag` lives in `prm.opt.flag` on the C++ struct
  (`pjsua_call_setting.flag`). The Python attribute was created on
  the SWIG proxy and silently ignored, so `pjsua_call_reinvite()`
  reused the cached hold-state SDP: the unhold re-INVITE went out
  with the same `o=` version and `a=sendonly`, the registrar
  ignored it as "no renegotiation" per RFC 3264, and media stayed
  one-way at the SDP layer despite local pjsua state reporting
  sendrecv. Fixed by writing to the correct C++ struct field
  (`prm.opt.flag = pj.PJSUA_CALL_UNHOLD`) AND populating
  `prm.opt.audioCount = 1` — `pj.CallOpParam()` defaults to
  audioCount=0/useDefaults=False, which would otherwise make pjsua
  emit an `m=audio 0` "rejected media" SDP with no rtpmap. Two
  regression tests added: a unit test (`TestUnholdSetsCallSettingFlag`
  — captures `prm.opt.flag` and `prm.opt.audioCount` on a fake call)
  and an integration test
  (`TestCodecs::test_unhold_flips_sdp_direction_and_bumps_version`
  — verifies the on-the-wire SDP version bumps and `a=sendonly` is
  dropped between hold and unhold INVITEs).

### Removed (BREAKING)
- MCP tools `get_codecs` and `set_codecs`. Replaced by per-phone API:
  - To set per-phone codecs on creation: `add_phone(codecs=[...])`.
  - To change at runtime: `update_phone(phone_id=..., codecs=[...])`.
  - Endpoint-wide pin no longer needed — startup pins the audio codec
    superset; per-phone SDP filter governs.
- Internal `CallManager.reinvite_with_codecs` (was the back-end for
  `set_codecs(phone_id=, call_id=)`).
- MCP tools `start_capture`, `stop_capture`, `get_pcap`. Per-phone auto-
  capture covers the common case via
  `update_phone(phone_id=..., capture_enabled=true/false)`. The pcap
  path is exposed in the recording's `.meta.json` sidecar (returned by
  `<phone>_get_recording(call_id)` and `list_recordings`).
- Internal `PcapManager.start`, `PcapManager.stop`,
  `PcapManager.get_pcap_info` (host-wide single-process capture). The
  per-phone surface (`start_for_phone`, `stop_for_phone`, etc.) remains.

### Changed
- `update_phone(codecs=[...])` now sends a re-INVITE on every CONFIRMED
  call of the phone so the live media stream swaps codec — symmetric
  with how `recording_enabled` toggles ongoing calls. Affected call
  IDs are returned in `codec_reinvited_call_ids`. Held calls get
  re-INVITEd as sendrecv (effectively unhold) — to preserve hold,
  unhold first or update codecs after unhold.

### Fixed
- `tests/_rtp_helpers.rtp_payload_types_in_pcap` now skips per-packet
  decode errors instead of raising — pcap may contain IPv6
  (WS-Discovery multicast) or malformed frames the IPv4 decoder
  doesn't accept.

### Added
- **Per-phone codec preferences via SDP rewrite.** Set
  `codecs: [PCMA, ...]` on `add_phone` / `update_phone` / YAML defaults
  to control the codecs that a specific phone advertises in SDP and
  uses for RTP. The endpoint pins all known audio codecs at startup
  (`enable_audio_codec_superset`); the per-phone filter in
  `Call.onCallSdpCreated` narrows what each phone offers/answers, and
  pjsua's media activation picks codecs from the SDP intersection — so
  RTP send/receive on a phone matches its `codecs` list.
- `src/sdp_rewriter.py` — pure-Python line-by-line audio codec filter
  with full edge-case coverage (multi-section SDP including video and
  T.38, hold direction, SRTP, dynamic-PT fmtp, static PT without
  rtpmap). 21 unit tests in `tests/test_sdp_rewriter.py`.
- `tests/_rtp_helpers.py` — pcap → RTP payload-type set extractor for
  integration tests, via `dpkt`. Supports DLT_LINUX_SLL/SLL2 (tcpdump
  `-i any`).
- Integration tests in `TestCodecs` covering: per-phone outbound offer,
  per-phone inbound answer, concurrent calls on different phones with
  different codecs, hold/unhold preservation, runtime mutation via
  `update_phone(codecs=...)`.

### Changed
- `update_phone(codecs=...)` is now per-phone and instant (no global
  side-effect). Use `set_codecs(...)` for endpoint-wide mid-call
  re-INVITEs only.
- `add_phone(codecs=...)` is wired to per-phone SDP filter; previously
  it secretly rewrote endpoint-wide priorities.

### Dependencies
- Added `dpkt>=1.9.8` to `requirements.txt` (pcap parsing in tests).
