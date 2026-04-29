# Changelog

## [Unreleased]

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
