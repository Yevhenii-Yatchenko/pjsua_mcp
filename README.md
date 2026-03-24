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
│  │  event loop)│  │  SIP MESSAGE) │  │   hold, xfer,  │  │
│  │             │  │               │  │   conference,  │  │
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

## MCP Tools (26)

### Setup & Registration

| Tool | Description |
|------|-------------|
| `configure` | Initialize SIP engine — transport (UDP/TCP/TLS), domain, credentials, SRTP, `auto_answer` flag |
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
| `get_call_info` | Get call state (CALLING, EARLY, CONFIRMED, DISCONNECTED), codec, duration, recording path |
| `get_call_history` | List completed calls with duration, status, codec, recording path |
| `send_dtmf` | Send DTMF digits on an active call |
| `hold` | Put a call on hold |
| `unhold` | Resume a held call |

### Call Transfer & Conference

| Tool | Description |
|------|-------------|
| `blind_transfer` | Blind transfer — send SIP REFER to redirect a call to another URI |
| `attended_transfer` | Attended transfer — bridge two active calls via REFER with Replaces |
| `conference` | Bridge multiple calls into a conference (3-way+) via the PJSUA2 conference bridge |

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

### 3. Use it

```
configure(domain="sip.example.com", username="alice", password="secret")
register()
make_call(dest_uri="sip:bob@sip.example.com")
get_call_info()
hangup()
get_sip_log(filter_text="INVITE")
```

## Call Scenarios

### Basic call

```
configure(domain="pbx.local", username="6001", password="secret")
register()
make_call(dest_uri="sip:6002@pbx.local")
# ... call is active, MOH plays automatically ...
hangup()
get_call_history()    # see completed call with recording path
```

### Auto-answer (IVR/bot mode)

```
configure(domain="pbx.local", username="6001", password="secret", auto_answer=True)
register()
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
attended_transfer(call_id=1, dest_call_id=2)        # bridge A↔C, B exits
```

### 3-way conference

```
make_call(dest_uri="sip:6002@pbx.local")           # call B
make_call(dest_uri="sip:6003@pbx.local")           # call C
conference(call_ids=[0, 1])                         # bridge all together
# All three parties can hear each other
```

### SIP messaging

```
send_message(dest_uri="sip:6002@pbx.local", body="Hello!")
get_messages()    # check received messages
```

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

32 tests covering all modules — log writer, engine guards, account config, call lookup, pcap file lookup, message queue, call history.

### Integration tests (self-contained)

The integration tests run a complete SIP environment inside Docker Compose — no external PBX required:

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner
```

18 integration tests using 2-3 MCP UA instances communicating via Asterisk PBX:

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
│  │  │  Registration, calls, transfers, conference, msgs     │   │   │
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
| `test_call_and_hangup` | Full call: INVITE → 200 OK → CONFIRMED → BYE |
| `test_callee_hangup` | Callee-initiated BYE |
| `test_sip_log_shows_invite` | INVITE appears in SIP log after call |
| `test_auto_answer` | auto_answer=True answers without manual answer_call |
| `test_reject_call` | Reject with 486 Busy, verify in caller's SIP log |
| `test_call_history` | Completed call appears in history with metadata |
| `test_send_and_receive_message` | SIP MESSAGE round-trip between two UAs |
| `test_get_messages_empty` | Empty message queue on fresh account |
| `test_send_message_without_registration` | Error when sending without register |
| `test_blind_transfer` | A↔B call, B sends REFER to redirect A to C |
| `test_attended_transfer` | A↔B, B holds, B↔C consult, B bridges A↔C via REFER/Replaces |
| `test_three_way_conference` | A calls B and C, bridges via conference bridge, all CONFIRMED |

## Project Structure

```
pjsua_mcp/
├── src/
│   ├── server.py            # MCP entry point, 26 tool definitions
│   ├── sip_engine.py        # PJSUA2 Endpoint lifecycle
│   ├── account_manager.py   # SIP registration, credentials, messaging
│   ├── call_manager.py      # Call control, transfer, conference, recording, playback
│   ├── sip_logger.py        # Custom LogWriter → bounded deque
│   └── pcap_manager.py      # tcpdump subprocess management
├── audio/
│   └── moh.wav              # Default MOH (CC0, FreeSWITCH/MUSOPEN)
├── tests/
│   ├── conftest.py           # Shared fixtures
│   ├── test_sip_logger.py    # 7 unit tests
│   ├── test_sip_engine.py    # 5 unit tests
│   ├── test_account_manager.py  # 11 unit tests
│   ├── test_call_manager.py  # 4 unit tests
│   ├── test_pcap_manager.py  # 5 unit tests
│   ├── test_integration.py   # 18 integration tests
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
- **Recording** — recorder connected AFTER player setup to avoid conference bridge disruption; reconnected on every `onCallMediaState` for robustness.
- **MOH** — Suite Espanola Op. 47 — Leyenda (Albeniz), classical guitar, CC0 public domain from FreeSWITCH/MUSOPEN.
