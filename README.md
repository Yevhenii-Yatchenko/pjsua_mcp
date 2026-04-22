# PJSUA MCP Server

An [MCP](https://modelcontextprotocol.io/) server that gives AI assistants full control over a SIP user agent. Built on [PJSUA2](https://www.pjsip.org/) (pjproject 2.14.1) with Python 3.13, packaged in Docker.

An AI can register with a SIP PBX, place and receive phone calls, transfer calls, set up conferences, send DTMF and text messages, record calls, play audio, inspect SIP logs, and capture packets — all through standard MCP tool calls.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  AI Assistant (Claude, etc.)                             │
│                                                          │
│  "Register as extension 6001, then call 6002"            │
└──────────────┬───────────────────────────────────────────┘
               │ MCP (JSON-RPC over stdio)
               ▼
┌──────────────────────────────────────────────────────────┐
│  PJSUA MCP Server (Docker)                               │
│                                                          │
│  ┌────────────┐  ┌───────────────┐  ┌────────────────┐  │
│  │ SipEngine   │  │ AccountManager│  │  CallManager   │  │
│  │ (Endpoint,  │  │ (credentials, │  │  (dial, answer,│  │
│  │  transport, │  │  REGISTER,    │  │   hangup, DTMF,│  │
│  │  codecs,    │  │  SIP MESSAGE) │  │   hold, xfer,  │  │
│  │  event loop)│  │               │  │   conference,  │  │
│  │             │  │               │  │   record, play)│  │
│  └──────┬─────┘  └───────┬───────┘  └───────┬────────┘  │
│         │                │                   │           │
│  ┌──────┴────────────────┴───────────────────┴────────┐  │
│  │              PJSUA2 / pjproject 2.14.1             │  │
│  └──────────────────────┬─────────────────────────────┘  │
│                         │ SIP/UDP                        │
│  ┌──────────────┐       │                                │
│  │ SipLogWriter  │ ◄────┘  (captures all SIP messages)   │
│  └──────────────┘                                        │
│  ┌──────────────┐                                        │
│  │ PcapManager   │  (tcpdump subprocess for pcap)        │
│  └──────────────┘                                        │
└──────────────────────────────────────────────────────────┘
               │ SIP/UDP
               ▼
        ┌─────────────┐
        │  SIP PBX /   │
        │  Registrar   │
        └─────────────┘
```

## MCP Tools (31)

### Setup & Registration

| Tool | Description |
|------|-------------|
| `setup` | **All-in-one**: configure + set codecs + register in a single call |
| `configure` | Initialize SIP engine — transport, domain, credentials, SRTP, `auto_answer`, `codecs` |
| `register` | Send SIP REGISTER to the configured registrar |
| `unregister` | Send un-REGISTER |
| `get_registration_status` | Check current registration state, status code, expiry |

### Call Control

| Tool | Description |
|------|-------------|
| `make_call` | Place an outbound call to a SIP URI, with optional custom headers |
| `answer_call` | Answer an incoming call (auto-selects first ringing, or by call ID) |
| `reject_call` | Reject an incoming call with SIP status code (486 Busy, 603 Decline) |
| `hangup` | Hang up a call |
| `get_call_info` | Call state, codec, duration, recording path, remote/local Contact, RTP stats |
| `get_active_calls` | **All active calls** with full info + RTP stats (no blind scanning) |
| `list_calls` | Compact summary of every tracked call_id with state |
| `get_call_history` | Completed calls with duration, status, codec, recording path |
| `send_dtmf` | Send DTMF digits on an active call |
| `hold` | Put a call on hold |
| `unhold` | Resume a held call |

### Call Transfer & Conference

| Tool | Description |
|------|-------------|
| `blind_transfer` | Blind transfer — send SIP REFER to redirect a call to another URI |
| `attended_transfer` | Attended transfer — bridge two active calls via REFER with Replaces |
| `conference` | Bridge multiple calls into a conference (3-way+) via the PJSUA2 conference bridge |

### Codec Management

| Tool | Description |
|------|-------------|
| `set_codecs` | Set codec priorities + optional re-INVITE to change codec mid-call |
| `get_codecs` | List all available codecs with current priorities |

### Audio Playback & Recording

| Tool | Description |
|------|-------------|
| `play_audio` | Play a WAV file into an active call (replaces current MOH) |
| `stop_audio` | Stop playback, resume default Music-on-Hold |
| `get_recording` | Get the WAV recording path/size for a call |
| `list_recordings` | List all call recordings |

### SIP Messaging

| Tool | Description |
|------|-------------|
| `send_message` | Send a SIP MESSAGE (instant text message) |
| `get_messages` | Get received SIP messages |

### Diagnostics

| Tool | Description |
|------|-------------|
| `get_sip_log` | Retrieve SIP signaling log — all entries, last N, or filtered by text |
| `start_capture` | Start tcpdump packet capture (pcap) |
| `stop_capture` | Stop capture, return file info |
| `get_pcap` | Get info about a capture file (most recent or by name) |

## Quick Start

### 1. Build the Docker image

```bash
docker compose build
```

### 2. Connect to an AI assistant

Add to your MCP client config (e.g. `.mcp.json`):

```json
{
  "mcpServers": {
    "pjsua": {
      "command": "docker",
      "args": ["compose", "run", "--rm", "-i", "pjsua-mcp"]
    }
  }
}
```

### 3. Describe your phones (YAML profile)

The server has no built-in SIP credentials — you describe your phones in a YAML profile. Two ways to get the template:

**(a) From MCP** — any AI client can ask the server directly:

```
mcp__pjsua__get_phone_profile_example()
```

This returns the template YAML plus a `save_to` hint. Save the `template` field as `./config/phones.yaml` on the host.

**(b) From the repo** — copy the tracked example:

```bash
cp config/phones.example.yaml config/phones.yaml
$EDITOR config/phones.yaml
```

`config/phones.yaml` is gitignored; only `phones.example.yaml` is tracked. The file gets mounted into the container at `/config/phones.yaml` (read-only) via docker-compose.

Minimal profile shape:

```yaml
defaults:                 # optional — merged into every phone, phone-level keys win
  domain: sip.example.com
  password: change_me
  codecs: [PCMA]
  auto_answer: false

phones:
  - phone_id: a
    username: "1001"
  - phone_id: b
    username: "1002"
    auto_answer: true
```

### 4. Load the profile and run scenarios

Once `./config/phones.yaml` exists:

```
mcp__pjsua__load_phone_profile()                   # default /config/phones.yaml
# → every phone listed in the file is registered and its per-phone tools
#   (a_make_call, b_hangup, …) become visible via tools/list_changed.

mcp__pjsua__a_make_call(dest_uri="sip:002@sip.example.com")
mcp__pjsua__a_get_call_info(call_id=0)
mcp__pjsua__a_hangup(call_id=0)
```

`load_phone_profile` is atomic replace by default: existing calls are hung up and existing phones are dropped before the new profile is applied. Pass `merge=True` to keep phones not listed in the new profile.

For ad-hoc additions without touching the profile:

```
mcp__pjsua__add_phone(phone_id="alice", domain="sip.example.com",
                      username="1099", password="x", codecs=["PCMA"])
mcp__pjsua__drop_phone(phone_id="alice")
```

## Call Scenarios

### Basic call

```
setup(domain="pbx.local", username="6001", password="secret")
make_call(dest_uri="sip:6002@pbx.local")
# ... MOH plays automatically, call is recorded ...
get_call_info()       # state, codec, duration, remote_contact, RTP stats
hangup()
get_call_history()    # completed call with recording path
```

### Auto-answer (IVR/bot mode)

```
setup(domain="pbx.local", username="6001", password="secret", auto_answer=True)
# Incoming calls are answered automatically with 200 OK
# play_audio("/app/audio/greeting.wav") to play a prompt
```

### Blind transfer

```
# B has active call with A, transfers A to C:
blind_transfer(dest_uri="sip:6003@pbx.local")
# B disconnects, A is now calling C
```

### Attended transfer

```
# B has active call with A:
hold(call_id=1)                                    # put A on hold
make_call(dest_uri="sip:6003@pbx.local")           # consult with C
attended_transfer(call_id=1, dest_call_id=2)        # bridge A<>C, B exits
```

### 3-way conference

```
make_call(dest_uri="sip:6002@pbx.local")           # call B
make_call(dest_uri="sip:6003@pbx.local")           # call C
conference(call_ids=[0, 1])                         # bridge all together
# All three parties can hear each other
```

### Codec selection & mid-call change

```
setup(domain="pbx.local", username="6001", password="x", codecs=["G722"])
make_call(dest_uri="sip:6002@pbx.local")
get_codecs()                                        # see current priorities
set_codecs(codecs=["PCMA"], call_id=0)              # re-INVITE with new codec
get_call_info()                                     # verify codec changed
```

### SIP messaging

```
send_message(dest_uri="sip:6002@pbx.local", body="Hello!")
get_messages()    # check received messages
```

### Monitoring active calls

```
get_active_calls()     # all active calls with RTP stats in one call
list_calls()           # compact summary including DISCONNECTED
get_call_history()     # completed calls with metadata
```

## Call Info & RTP Statistics

`get_call_info` returns comprehensive call data including real-time RTP statistics:

```json
{
  "call_id": 0,
  "state": "CONFIRMED",
  "remote_uri": "sip:6002@pbx",
  "remote_contact": "<sip:6002@172.20.0.3:5060;ob>",
  "local_contact": "<sip:6001@172.20.0.2:5060>",
  "codec": "PCMA",
  "duration": 45,
  "recording_file": "/recordings/call_0_20260407_141603.wav",
  "playing_file": "/app/audio/moh.wav",
  "rtp": {
    "tx_packets": 2250,
    "tx_bytes": 360000,
    "rx_packets": 2248,
    "rx_bytes": 359680,
    "rx_loss": 0,
    "rx_dup": 0,
    "rx_reorder": 0,
    "rx_discard": 0,
    "rx_jitter_usec": 875,
    "rtt_usec": 6362
  }
}
```

`get_active_calls` returns this data for all active calls at once — no need to scan call_ids.

## Call Recording & Audio

Every call is **automatically recorded** to `/recordings/call_{id}_{timestamp}.wav` (both local and remote audio mixed into one mono file). Files are accessible on the host via the `./recordings/` volume mount.

**Music-on-Hold** plays automatically when a call connects — Suite Espanola Op. 47 (Albeniz), CC0 public domain from FreeSWITCH/MUSOPEN, 8kHz WAV. Use `play_audio` to override with custom audio, `stop_audio` to resume MOH.

## SIP Log Inspection

Every SIP message processed by the PJSUA2 stack is captured by a custom `LogWriter` into a bounded in-memory deque (5000 entries):

```
get_sip_log()                              # all entries
get_sip_log(last_n=20)                     # last 20
get_sip_log(filter_text="REGISTER")        # only REGISTER-related
get_sip_log(filter_text="401")             # find auth challenges
```

Each entry contains:
- `level` — pjsip log level (1=error ... 5=trace)
- `msg` — the full log line (includes SIP message dumps)
- `thread` — originating pjlib thread name

## Testing

### Unit tests (fast, no network)

```bash
docker compose run --rm --entrypoint pytest pjsua-mcp tests/ -m "not integration" -v
```

33 tests covering all modules — log writer, engine guards, account config, call lookup, pcap file lookup, message queue, call history, codecs.

### Integration tests (self-contained)

The integration tests run a complete SIP environment inside Docker Compose — no external PBX required:

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner
```

22 integration tests using 2-3 MCP UA instances communicating via Asterisk PBX:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Docker Compose network: sipnet                                     │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  test-runner container                                       │   │
│  │                                                              │   │
│  │  pytest spawns 2-3 MCP server subprocesses:                  │   │
│  │                                                              │   │
│  │  ┌────────────────┐ ┌────────────────┐ ┌────────────────┐   │   │
│  │  │ MCP Server A   │ │ MCP Server B   │ │ MCP Server C   │   │   │
│  │  │ (ext 6001)     │ │ (ext 6002)     │ │ (ext 6003)     │   │   │
│  │  └───────┬────────┘ └───────┬────────┘ └───────┬────────┘   │   │
│  │          │ JSON-RPC/stdio   │                   │            │   │
│  │          ▼                  ▼                   ▼            │   │
│  │  ┌──────────────────────────────────────────────────────┐   │   │
│  │  │  pytest test orchestrator                             │   │   │
│  │  │  Registration, calls, transfers, conference, msgs,    │   │   │
│  │  │  codecs, auto-answer, reject, call history            │   │   │
│  │  └──────────────────────────────────────────────────────┘   │   │
│  └──────────────────────────┬──────────────────────────────────┘   │
│                    SIP/UDP  │                                       │
│                             ▼                                       │
│                  ┌────────────────────┐                              │
│                  │  Asterisk PBX      │                              │
│                  │  ext 6001/6002/6003│                              │
│                  └────────────────────┘                              │
└─────────────────────────────────────────────────────────────────────┘
```

#### What the tests verify

| Test | What it proves |
|------|----------------|
| `test_configure_and_register` | Full SIP registration: REGISTER, 401 challenge, auth, 200 OK |
| `test_get_registration_status` | Registration state is queryable |
| `test_register_without_configure` | Proper error handling |
| `test_get_sip_log_has_entries` | SIP log captures real signaling |
| `test_get_sip_log_filter` | Text filtering works |
| `test_unregister` | Clean un-REGISTER |
| `test_call_and_hangup` | Full call: INVITE, 200 OK, CONFIRMED, BYE |
| `test_callee_hangup` | Callee-initiated BYE |
| `test_sip_log_shows_invite` | INVITE appears in SIP log after call |
| `test_auto_answer` | auto_answer=True answers without manual answer_call |
| `test_call_info_contacts` | remote_contact and local_contact returned |
| `test_reject_call` | Reject with 486 Busy, verify in caller's SIP log |
| `test_call_history` | Completed call appears in history with metadata |
| `test_send_and_receive_message` | SIP MESSAGE round-trip between two UAs |
| `test_get_messages_empty` | Empty message queue on fresh account |
| `test_send_message_without_registration` | Error when sending without register |
| `test_blind_transfer` | A calls B, B sends REFER to redirect A to C |
| `test_attended_transfer` | A calls B, B holds, B calls C, B bridges A to C |
| `test_three_way_conference` | A calls B and C, bridges via conference bridge |
| `test_configure_with_codecs` | Configure with specific codec, verify in call |
| `test_get_codecs` | List codecs with priorities |
| `test_set_codecs_midcall` | Change codec mid-call via re-INVITE |

## Project Structure

```
pjsua_mcp/
├── src/
│   ├── server.py            # MCP entry point, 31 tool definitions
│   ├── sip_engine.py        # PJSUA2 Endpoint lifecycle, codec management
│   ├── account_manager.py   # SIP registration, credentials, messaging
│   ├── call_manager.py      # Call control, transfer, conference, recording, playback
│   ├── sip_logger.py        # Custom LogWriter -> bounded deque
│   └── pcap_manager.py      # tcpdump subprocess management
├── audio/
│   └── moh.wav              # Default MOH (CC0, FreeSWITCH/MUSOPEN)
├── tests/
│   ├── conftest.py           # Shared fixtures
│   ├── test_sip_logger.py    # 7 unit tests
│   ├── test_sip_engine.py    # 6 unit tests
│   ├── test_account_manager.py  # 11 unit tests
│   ├── test_call_manager.py  # 4 unit tests
│   ├── test_pcap_manager.py  # 5 unit tests
│   ├── test_integration.py   # 22 integration tests
│   └── asterisk/
│       ├── Dockerfile        # Asterisk PBX image for testing
│       ├── pjsip.conf        # Three extensions (6001-6003) with digest auth
│       ├── extensions.conf   # Dialplan routing + SIP MESSAGE relay
│       └── modules.conf      # PJSIP modules config
├── Dockerfile                # Multi-stage: build pjproject + runtime
├── docker-compose.yml        # Production: MCP server with host networking
├── docker-compose.test.yml   # Testing: Asterisk + test runner on isolated network
├── requirements.txt          # mcp, pydantic, pytest
├── pyproject.toml            # Pytest config (markers, pythonpath)
└── .mcp.json                 # MCP client config for AI assistants
```

## Technical Notes

- **Python 3.13 + pjproject 2.14.1** — built from source in a multi-stage Docker build. Python 3.13 removed `distutils`, so `setuptools` is installed before building the SWIG bindings.
- **Null audio device** — runs headless in Docker with no sound card. ALSA library is still linked at runtime.
- **Threading model** — `threadCnt=0` with manual event loop polling from the asyncio thread (~50 polls/sec). SWIG director callbacks (LogWriter) don't work reliably from executor threads.
- **stdout protection** — C-level fd 1 is redirected to stderr at startup. MCP JSON-RPC uses a saved copy of the original stdout fd. This prevents pjlib console output from corrupting the MCP channel.
- **SIP log** — `consoleLevel=5` (matching `level=5`) ensures the global log level isn't suppressed. The LogWriter captures everything into a thread-safe bounded deque.
- **Auto-answer** — deferred to the event poll loop (not inside `onIncomingCall` callback) to avoid PJSUA2 call state machine issues.
- **Recording** — recorder connected AFTER player setup to avoid conference bridge disruption; reconnected on every `onCallMediaState` for robustness. Both local and remote audio mixed into one mono WAV.
- **Re-INVITE handling** — audio player is reconnected to the new `aud_med` port after re-INVITE (codec change, conference conversion) to maintain TX.
- **Stale call cleanup** — disconnected calls are removed from tracking; accounts are shut down before re-registration to prevent ghost sessions.
- **MOH** — Suite Espanola Op. 47 — Leyenda (Albeniz), classical guitar, CC0 public domain from FreeSWITCH/MUSOPEN.
