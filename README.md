# PJSUA MCP Server

An [MCP](https://modelcontextprotocol.io/) server that gives AI assistants full control over a SIP user agent. Built on [PJSUA2](https://www.pjsip.org/) (pjproject 2.14.1) with Python 3.13, packaged in Docker.

An AI can register with a SIP PBX, place and receive phone calls, send DTMF, put calls on hold, inspect SIP signaling logs, and capture packets — all through standard MCP tool calls.

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
│  │  transport, │  │  REGISTER)    │  │   hangup, DTMF,│  │
│  │  event loop)│  │               │  │   hold/unhold) │  │
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

## MCP Tools

### Setup & Registration

| Tool | Description |
|------|-------------|
| `configure` | Initialize the SIP engine — set transport (UDP/TCP/TLS), domain, credentials, SRTP |
| `register` | Send SIP REGISTER to the configured registrar |
| `unregister` | Send un-REGISTER |
| `get_registration_status` | Check current registration state, status code, expiry |

### Call Control

| Tool | Description |
|------|-------------|
| `make_call` | Place an outbound call to a SIP URI, with optional custom headers |
| `answer_call` | Answer an incoming call (auto-selects first ringing call, or by call ID) |
| `hangup` | Hang up a call |
| `get_call_info` | Get call state (CALLING, EARLY, CONFIRMED, DISCONNECTED), codec, duration |
| `send_dtmf` | Send DTMF digits on an active call |
| `hold` | Put a call on hold |
| `unhold` | Resume a held call |

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

The AI assistant can now:

```
configure(domain="sip.example.com", username="alice", password="secret")
register()
make_call(dest_uri="sip:bob@sip.example.com")
get_call_info()
hangup()
get_sip_log(filter_text="INVITE")
```

## SIP Log Inspection

Every SIP message processed by the PJSUA2 stack is captured by a custom `LogWriter` into a bounded in-memory deque (5000 entries). The AI can query this log at any time to understand what happened on the wire:

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

This lets an AI debug SIP issues, verify authentication flows, inspect SDP negotiation, and understand call state transitions — without needing to read raw pcap files.

## Testing

### Unit tests (fast, no network)

```bash
docker compose run --rm --entrypoint pytest pjsua-mcp tests/ -m "not integration" -v
```

25 tests covering all modules — log writer, engine guards, account config, call lookup, pcap file lookup.

### Integration tests (self-contained)

The integration tests run a complete SIP environment inside Docker Compose — no external PBX required:

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner
```

#### How it works

```
┌─────────────────────────────────────────────────────────────────────┐
│  Docker Compose network: sipnet                                     │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  test-runner container                                       │   │
│  │                                                              │   │
│  │  pytest spawns two MCP server subprocesses:                  │   │
│  │                                                              │   │
│  │  ┌───────────────────┐      ┌───────────────────┐           │   │
│  │  │ MCP Server A      │      │ MCP Server B      │           │   │
│  │  │ (ext 6001/caller) │      │ (ext 6002/callee) │           │   │
│  │  │                   │      │                   │           │   │
│  │  │ SIP UA ──────────────┐ ┌────────────── SIP UA│           │   │
│  │  └───────────────────┘  │ │  └───────────────────┘           │   │
│  │     ▲  JSON-RPC/stdio   │ │     ▲  JSON-RPC/stdio            │   │
│  │     │                   │ │     │                            │   │
│  │     ▼                   │ │     ▼                            │   │
│  │  ┌──────────────────────┼─┼─────────────────────────────┐   │   │
│  │  │        pytest test orchestrator                       │   │   │
│  │  │                                                       │   │   │
│  │  │  1. configure + register both UAs                     │   │   │
│  │  │  2. A calls B  (make_call → sip:6002@asterisk)        │   │   │
│  │  │  3. B answers   (answer_call)                         │   │   │
│  │  │  4. verify CONFIRMED state (get_call_info)            │   │   │
│  │  │  5. hangup                                            │   │   │
│  │  │  6. inspect SIP log (get_sip_log filter_text=INVITE)  │   │   │
│  │  └──────────────────────┼─┼─────────────────────────────┘   │   │
│  └──────────────────────────┼─┼──────────────────────────────────┘   │
│                             │ │                                      │
│                    SIP/UDP  │ │  SIP/UDP                             │
│                             ▼ ▼                                      │
│                  ┌────────────────────┐                              │
│                  │  Asterisk PBX      │                              │
│                  │  (andrius/asterisk)│                              │
│                  │                    │                              │
│                  │  ext 6001 ◄──────► │                              │
│                  │  ext 6002 ◄──────► │                              │
│                  │                    │                              │
│                  │  Dialplan:         │                              │
│                  │  6001 → Dial(6001) │                              │
│                  │  6002 → Dial(6002) │                              │
│                  └────────────────────┘                              │
└─────────────────────────────────────────────────────────────────────┘
```

#### Call flow sequence

```
  pytest            MCP Server A (6001)      Asterisk        MCP Server B (6002)
    │                      │                    │                     │
    ├─ configure(6001) ──► │                    │                     │
    ├─ register ─────────► │ ── REGISTER ─────► │                     │
    │                      │ ◄─ 200 OK ──────── │                     │
    │                      │                    │                     │
    ├─ configure(6002) ──────────────────────────────────────────────► │
    ├─ register ─────────────────────────────────────────────────────► │
    │                      │                    │ ◄── REGISTER ────── │
    │                      │                    │ ──► 200 OK ───────► │
    │                      │                    │                     │
    ├─ make_call(6002) ──► │                    │                     │
    │                      │ ── INVITE ───────► │                     │
    │                      │                    │ ── INVITE ────────► │
    │                      │ ◄─ 100 Trying ──── │                     │
    │                      │                    │ ◄─ 180 Ringing ──── │
    │                      │ ◄─ 180 Ringing ─── │                     │
    │                      │                    │                     │
    ├─ answer_call ──────────────────────────────────────────────────► │
    │                      │                    │ ◄── 200 OK ──────── │
    │                      │ ◄── 200 OK ─────── │                     │
    │                      │ ── ACK ──────────► │ ──► ACK ──────────► │
    │                      │                    │                     │
    │               location CONFIRMED     ◄── RTP media ──►     CONFIRMED
    │                      │                    │                     │
    ├─ get_call_info ────► │                    │                     │
    │  state=CONFIRMED     │                    │                     │
    │                      │                    │                     │
    ├─ hangup ───────────► │                    │                     │
    │                      │ ── BYE ──────────► │                     │
    │                      │                    │ ── BYE ───────────► │
    │                      │ ◄─ 200 OK ──────── │                     │
    │                      │                    │                     │
    ├─ get_sip_log ──────► │                    │                     │
    │  (filter=INVITE)     │                    │                     │
    │  → entries with      │                    │                     │
    │    INVITE messages   │                    │                     │
```

#### What the tests verify

| Test | What it proves |
|------|----------------|
| `test_configure_and_register` | Full SIP registration flow: engine init, REGISTER, 401 challenge, auth, 200 OK |
| `test_get_registration_status` | Registration state is queryable after setup |
| `test_register_without_configure` | Proper error when calling register before configure |
| `test_get_sip_log_has_entries` | SIP log captures real signaling after registration |
| `test_get_sip_log_filter` | Text filtering works (e.g. only REGISTER messages) |
| `test_unregister` | Clean un-REGISTER |
| `test_call_and_hangup` | Full call: INVITE, 200 OK, CONFIRMED state, BYE from caller |
| `test_callee_hangup` | Callee-initiated hangup (BYE from the other side) |
| `test_sip_log_shows_invite` | SIP log contains INVITE entries after a call |

## Project Structure

```
pjsua_mcp/
├── src/
│   ├── server.py            # MCP entry point, 15 tool definitions
│   ├── sip_engine.py        # PJSUA2 Endpoint lifecycle
│   ├── account_manager.py   # SIP registration and credentials
│   ├── call_manager.py      # Call control (dial, answer, DTMF, hold)
│   ├── sip_logger.py        # Custom LogWriter → bounded deque
│   └── pcap_manager.py      # tcpdump subprocess management
├── tests/
│   ├── conftest.py           # Shared fixtures (pjsua_endpoint, tmp_captures_dir)
│   ├── test_sip_logger.py    # 7 unit tests
│   ├── test_sip_engine.py    # 5 unit tests
│   ├── test_account_manager.py  # 5 unit tests
│   ├── test_call_manager.py  # 3 unit tests
│   ├── test_pcap_manager.py  # 5 unit tests
│   ├── test_integration.py   # 9 integration tests (registration + call flows)
│   └── asterisk/
│       ├── Dockerfile        # Asterisk PBX image for testing
│       ├── pjsip.conf        # Two extensions (6001, 6002) with digest auth
│       ├── extensions.conf   # Dialplan routing between extensions
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
