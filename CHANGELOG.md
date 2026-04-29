# Changelog

## [Unreleased]

### Added
- `run_scenario` result now includes an `artifacts: {phone_id: {...}}` dict
  populated post-stop. Each phone in `scenario.phones` maps to either
  `null` (no recording/pcap created during this run) or a dict with
  `recording`, `recording_meta`, `pcap` (container paths) and
  `host_recording`, `host_recording_meta`, `host_pcap` (host-side paths
  when `PJSUA_MCP_HOST_RECORDINGS_DIR` / `PJSUA_MCP_HOST_CAPTURES_DIR`
  are set in the env, otherwise `null`). Files are filtered by
  `mtime ≥ scenario.started_at` so a run never picks up a previous
  run's artifacts; latest-mtime wins per phone. Backward-compatible —
  existing fields (`status`, `elapsed_ms`, `timeline`, `errors`,
  `reason`) unchanged.
- `src/scenario_engine/artifacts.py:collect_artifacts(...)` — pure
  helper backing the above. 12 unit tests in
  `tests/scenario_engine/test_artifacts.py` cover mtime filter,
  latest-wins, pair-with-meta-sidecar, host path mapping, missing-
  directory tolerance, recording-disabled-pcap-only edge case.
- Integration tests `TestRunScenarioArtifacts` in
  `tests/test_integration.py` covering proposal-04 acceptance criteria
  against live Asterisk: artifacts populated for both legs of a
  roundtrip, two sequential runs report distinct recordings,
  unengaged phones come back as null.

### Configuration
- New env vars `PJSUA_MCP_HOST_RECORDINGS_DIR` and
  `PJSUA_MCP_HOST_CAPTURES_DIR` (both optional). When set, the host-
  side absolute paths of the `/recordings` and `/captures` bind targets
  are surfaced in `run_scenario.artifacts.<phone>.host_*` fields so
  out-of-container clients (Claude Code, plugin host) can Read/Bash
  artifacts without resolving the bind mount themselves. Empty / unset
  → `host_*` fields come back as `null`. `docker-compose.yml` passes
  both through; `.env.example` documents the wiring.

### Added
- MCP tool `get_call_messages(phone_id, call_id, method, direction,
  status_code, cseq, last_n)` — structured counterpart to `get_sip_log`.
  Returns `{messages: [{ts, direction, method, cseq, call_id, from, to,
  headers, sdp, status_code?}, ...]}` with parsed SDP (codecs, media
  ports, direction, rtcp port). Built for LLM-driven plan checks
  ("did alice's INVITE offer PCMU only?") that previously required
  regex-grepping raw SIP messages. Drops non-SIP log entries (pjlib
  library logs, `[DISCONNECTED]` dumps) silently.
- `get_sip_log` accepts new filter kwargs `method`, `direction`,
  `status_code`, `cseq` — same set as `get_call_messages` so both tools
  share semantics. `get_sip_log` keeps `filter_text` (substring escape
  hatch); `get_call_messages` does not (parsed structure makes it
  unnecessary).
- `src/sip_logger.py:parse_sdp_body(text)` — line-by-line SDP parser
  per RFC 4566. Output shape per proposal-03: `{version, origin, media:
  [{type, port, protocol, payload_types, codecs:[{pt, name, clock_rate,
  fmtp?}], direction, rtcp_port}]}`. Tolerates CRLF/LF endings, missing
  optional lines (b=, a=ssrc, a=rtcp), static PT without rtpmap (PT 0/8/9
  default to PCMU/PCMA/G722 per RFC 3551), multi-section SDP. 14 unit
  tests in `tests/test_sip_logger.py::TestParseSdpBody`.
- `src/sip_logger.py:parse_sip_headers(text)` — extracts SIP message
  headers as `dict[str, str | list[str]]`. Multi-value headers (Via x2,
  Route, Record-Route) collapse into lists. Canonical case for well-known
  headers (Call-ID, CSeq, Content-Type, etc.). 11 unit tests in
  `tests/test_sip_logger.py::TestParseSipHeaders`.
- `src/sip_logger.py:structurize_message(entry)` — composes
  `parse_sip_metadata + parse_sip_headers + parse_sdp_body` plus a small
  timestamp regex into the proposal-03 message shape. Returns None for
  non-SIP entries. 9 unit tests in
  `tests/test_sip_logger.py::TestStructurizeMessage`. Integration tests
  in `TestCallFlow` (5 new) cover `get_call_messages` against live
  Asterisk: parsed SDP codec list, call_id filter narrowing, status_code
  filter, non-SIP entry exclusion, unknown call_id warning.

### Fixed
- `parse_sip_metadata` now extracts `from_uri`/`to_uri` from both name-addr
  (`From: <sip:user@host>;tag=X`) and addr-spec (`From: sip:user@host;
  tag=X`) forms — previously only the bracketed form was recognized,
  leaving the field None for some proxy-routed messages. Two unit tests
  added covering bare-URI and display-name shapes.

### Changed (BREAKING for fragile substring consumers)
- `get_sip_log(phone_id=...)` now resolves message ownership structurally
  instead of substring-matching `sip:<username>@`. Messages are attributed
  to the phone whose tracker holds their SIP Call-ID, whose local
  transport port appears in a `Via:` line, or whose username is the
  REGISTER From URI. Cross-leg false positives (bob's RX INVITE with
  `From: <sip:alice@>`, bob's `[DISCONNECTED]` dump showing alice's URI
  in `To: <...>`) no longer leak into alice's filtered log. Entries
  whose owner cannot be resolved structurally fall back to substring
  match and the response surfaces a `warning` field listing the count.
- `get_sip_log` accepts a new `call_id: int | None` parameter. With it,
  the response is restricted to the SIP dialog of the named pjsua-internal
  call (matched on Call-ID header). Combinable with `phone_id`. Unknown
  call_id returns `total_count=0` and a `warning`, never an error.
- `CallManager` now keeps a `(SIP Call-ID → owner)` index populated in
  `make_call` and `_on_incoming_call` from `pj.CallInfo.callIdString`.
  Entries persist past disconnect so historical log queries still
  resolve correctly.

### Added
- `src/sip_logger.py:parse_sip_metadata(msg)` — best-effort structured
  pull from a pjlib log entry: direction (TX/RX), method, cseq,
  status_code, SIP Call-ID, From/To URIs, Via ports, plus the special
  `dump_remote_uri` field for pjsua's `[<STATE>] To: <URI>` call dump.
  Pure function, no pjsua dependency. 12 unit tests in
  `tests/test_sip_logger.py::TestParseSipMetadata`.
- `src/sip_logger.py:filter_entries_by_owner(entries, phones, ...)` —
  pure ownership-based filter that powers the new `get_sip_log`. 10
  unit tests cover each ownership signal (Call-ID, dump URI, Via port,
  REGISTER username, fallback substring), URI-format normalization, and
  the `target_sip_call_id` intersection. 4 unit tests for the
  `CallManager` Call-ID tracker, plus 3 integration tests in
  `TestCallFlow` covering the proposal-05 acceptance criteria
  (no-leak of `[DISCONNECTED]` dump, `call_id` filter narrowing,
  unknown `call_id` warning).

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
