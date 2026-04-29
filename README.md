# PJSUA MCP Server

An [MCP](https://modelcontextprotocol.io/) server that gives AI assistants control over many SIP user agents at once. Built on [PJSUA2](https://www.pjsip.org/) (pjproject 2.14.1) with Python 3.13, packaged in Docker.

One MCP server process manages N phones side by side. Each phone gets its own `pj.Account` and its own UDP transport inside a single `pj.Endpoint`. When you add a phone the server registers 22 per-phone action tools (`<phone_id>_make_call`, `<phone_id>_hangup`, …) via `mcp.add_tool()` and fires `notifications/tools/list_changed`; when you drop the phone those tools disappear again.

On top of those atomic tools, the server ships an **event-driven scenario engine** (`src/scenario_engine/`) — an LLM agent describes a multi-step SIP flow once as a YAML scenario, and the engine executes the whole thing inside its own asyncio loop without per-step LLM-turn latency. Scenarios compose atomic **patterns** (14 ship out-of-the-box) or define inline `hooks:` for one-off flows, and return a complete timeline of every event + action for post-mortem inspection. See the **Scenario Engine** section below.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  AI Assistant (Claude, etc.)                                 │
│                                                              │
│  "Load the test profile, then call from a to 002"            │
└──────────────┬───────────────────────────────────────────────┘
               │ MCP (JSON-RPC over stdio)
               ▼
┌──────────────────────────────────────────────────────────────┐
│  PJSUA MCP Server (one Docker container)                     │
│                                                              │
│  ┌───────────────────── scenario engine ─────────────────┐   │
│  │ EventBus ◄── emit reg.* / call.state.* / dtmf.* / im  │   │
│  │    ▲           from pjsua callbacks                   │   │
│  │    │                                                  │   │
│  │ HookRuntime                    ActionExecutor         │   │
│  │    │                            (maps 19 actions       │   │
│  │    │                             to CallManager etc.)  │   │
│  │    │                                                   │   │
│  │ Orchestrator ──► run_scenario / validate_scenario /    │   │
│  │                  get_scenario_template                 │   │
│  └──────────────────────────────────────────────────────┘   │
│                       ▼                                      │
│  ┌────────────┐  ┌───────────────┐  ┌─────────────────────┐  │
│  │ SipEngine  │  │ PhoneRegistry │  │     CallManager     │  │
│  │ (Endpoint, │  │  dict[pid]    │  │  dict[call_id],     │  │
│  │  codecs,   │  │  → SipAccount │  │  per-phone queues,  │  │
│  │  per-phone │  │   + Config    │  │  incoming routing,  │  │
│  │  transports)│  │               │  │  always-on recording│  │
│  └──────┬─────┘  └──────┬────────┘  └──────┬──────────────┘  │
│         │               │                   │                │
│         │          ┌────┴─── phone_tool_factory ─────┐       │
│         │          │  register_phone_tools(mcp, pid) │       │
│         │          │    → 22 closures per phone      │       │
│         │          │    → mcp.add_tool / remove_tool │       │
│         │          └─────────────────────────────────┘       │
│         │                                                    │
│  ┌──────┴──────────────────────────────────────────────────┐ │
│  │              PJSUA2 / pjproject 2.14.1                  │ │
│  └──────────────────────┬──────────────────────────────────┘ │
│                         │ SIP/UDP (1 socket per phone)       │
│  ┌──────────────┐       │                                    │
│  │ SipLogWriter  │ ◄────┘  captures every SIP message        │
│  └──────────────┘                                            │
│  ┌──────────────┐                                            │
│  │ PcapManager   │  tcpdump — host-wide or BPF per phone     │
│  └──────────────┘                                            │
└──────────────────────────────────────────────────────────────┘
               │ SIP/UDP
               ▼
        ┌─────────────┐
        │  SIP PBX /   │
        │  Registrar   │
        └─────────────┘
```

## MCP Tools

### Static (17 — always present)

#### Phone CRUD
| Tool | Description |
|------|-------------|
| `list_phones` | All registered phones with registration state, transport port, active-call count, per-phone tool names |
| `add_phone` | Create a transport + SipAccount, send REGISTER, register 22 per-phone action tools |
| `drop_phone` | Hang up the phone's calls, unregister, close transport, unload its per-phone tools |
| `get_phone` | Full info for one phone — credentials (sans password), reg state, active calls, `recording_enabled` |
| `update_phone` | Mutate runtime settings — `auto_answer` / `recording_enabled` / `capture_enabled` (instant), `codecs`, or credentials (forces reregister) |
| `load_phones` | Bulk-add every phone listed in a YAML profile. Atomic replace by default (`merge=True` for upsert) |
| `get_phone_profile_example` | Return a ready-to-edit YAML template, host/container paths, and next-step hints |

#### Global diagnostics
| Tool | Description |
|------|-------------|
| `get_codecs` | List endpoint codec priorities |
| `set_codecs` | Set endpoint codec priorities (affects all phones). With `phone_id` + `call_id` — re-INVITE one call |
| `get_sip_log` | Retrieve pjsip log entries. `phone_id=...` filters by that phone's `sip:<user>@` URI substring |
| `start_capture` | Start tcpdump. Without `phone_id` — host-wide; with `phone_id` — BPF filter on that phone's UDP port |
| `stop_capture` | Stop the running capture |
| `get_pcap` | Info about the most recent (or named) pcap file |
| `list_recordings` | Walk `/recordings/` (and legacy flat files) for every WAV; filter by `phone_id` / `call_id` |

#### Scenario engine
| Tool | Description |
|------|-------------|
| `get_scenario_template` | Ready-to-edit scenario YAML skeleton + structured reference of every event type and action |
| `validate_scenario` | Static dry-run — catches unknown actions, unknown event types, malformed hooks — without touching pjsua |
| `run_scenario` | Execute a scenario (dict or YAML path), auto-validates first, returns full timeline + status + errors |

### Per-phone dynamic (22 per active phone)

Registered when `add_phone` (or `load_phones`) brings a phone online; unregistered on `drop_phone`. Examples below use phone `a`:

| Tool | Description |
|------|-------------|
| `a_make_call` | Outbound INVITE with optional custom SIP headers |
| `a_answer_call` | Answer an incoming call on phone a (auto-selects first queued if `call_id` omitted) |
| `a_reject_call` | Reject with a SIP status code (486 / 603 / 480) |
| `a_hangup` | BYE an active call |
| `a_get_call_info` | State, codec, duration, RTP stats, remote/local Contact, recording path |
| `a_get_call_history` | Completed calls on phone a |
| `a_list_calls` | Compact state summary of a's tracked calls |
| `a_get_active_calls` | Active calls with full info + RTP |
| `a_send_dtmf` | Send DTMF digits on a's call |
| `a_hold` / `a_unhold` | Re-INVITE sendonly / resume |
| `a_blind_transfer` | REFER to redirect a's call |
| `a_attended_transfer` | REFER+Replaces. Both legs must belong to phone a — cross-phone bridging is rejected |
| `a_conference` | Bridge multiple a-owned calls into a conference |
| `a_play_audio` / `a_stop_audio` | Play WAV into a call / resume MOH |
| `a_get_recording` | Path/size of the WAV + sidecar meta for a call on phone a |
| `a_send_message` / `a_get_messages` | SIP MESSAGE outbox / inbox |
| `a_register` / `a_unregister` | Fresh REGISTER cycle / de-REGISTER (symmetric pair) |
| `a_get_registration_status` | Quick reg state for phone a |

Total surface with N phones: 17 + 22·N.

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
      "args": ["compose", "-f", "/absolute/path/to/pjsua_mcp/docker-compose.yml",
               "run", "--rm", "-i", "pjsua-mcp"]
    }
  }
}
```

### 3. Describe your phones (YAML profile)

The server ships with no SIP credentials — you describe your phones in a YAML profile that stays on your host.

**(a) From MCP** — any AI client can ask for the template directly:

```
mcp__pjsua__get_phone_profile_example()
```

Returns the template YAML, `save_to` hint, and `next_step` tool name.

**(b) From the repo:**

```bash
cp config/phones.example.yaml config/phones.yaml
$EDITOR config/phones.yaml
```

`config/phones.yaml` is gitignored; only `phones.example.yaml` is tracked. docker-compose bind-mounts `./config` → `/config` (read-only).

Minimal profile:

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

```
mcp__pjsua__load_phones()                   # reads /config/phones.yaml
# → every phone registers; a_make_call, b_hangup, … appear via tools/list_changed.

mcp__pjsua__a_make_call(dest_uri="sip:002@sip.example.com")
mcp__pjsua__a_get_call_info(call_id=0)
mcp__pjsua__a_hangup(call_id=0)
```

`load_phones` is **atomic replace** by default: before loading, every existing phone's active calls are hung up and the phones are dropped. Pass `merge=True` to keep phones that aren't listed in the new profile.

For ad-hoc additions without touching the profile file:

```
mcp__pjsua__add_phone(phone_id="alice",
                      domain="sip.example.com",
                      username="1099", password="x",
                      codecs=["PCMA"])
# → /recordings/alice/ starts receiving WAVs on every call.
mcp__pjsua__drop_phone(phone_id="alice")
```

## Call Scenarios — two styles

The server supports two modes for driving calls:

- **Atomic-tool mode** — call per-phone tools directly (`a_make_call`,
  `b_answer_call`, …) and poll between steps with `time.sleep`. Good for
  interactive debugging and one-off experiments.
- **Scenario-engine mode** — describe the whole flow as a YAML scenario and
  hand it to `run_scenario`. The engine arms hooks against its event bus
  and drives the flow in one tight asyncio loop, so timings are
  deterministic and there's no per-step LLM-turn latency. Good for
  reproducible test cases and ticket reproducers.

The examples below show the engine form (preferred for repeatable work);
the atomic-tool equivalent is always available as a fallback.

All examples assume the profile is already loaded (`a`, `b`, `c` online).
Replace URIs with your registrar's.

### Basic call: A → B with DTMF

```yaml
# scenario
name: a-to-b-dtmf
phones: [a, b]
patterns:
  - {use: auto-answer, phone_id: b, delay_ms: 500}
  - {use: send-dtmf-on-confirmed, phone_id: a, digits: "1234"}
  - {use: hangup-after-duration, phone_id: a, duration_ms: 5000}
  - {use: make-call-and-wait-confirmed, phone_id: a,
     dest_uri: "sip:002@sip.example.com"}
stop_on: [{phone_id: a, event: call.state.disconnected}]
timeout_ms: 15000
```

```python
run_scenario(scenario=<dict above>)
# Returns: {status: "ok", timeline: [...events + actions with ms offsets...]}
```

Atomic-tool equivalent:

```
a_make_call(dest_uri="sip:002@sip.example.com")
# b auto-answers (auto_answer: true in profile)
a_send_dtmf(call_id=0, digits="1234")
a_hangup(call_id=0)
```

### Auto-answer (IVR / bot mode)

Set `auto_answer: true` for a phone in YAML or toggle at runtime:
```
update_phone(phone_id="b", auto_answer=True)
```
…or build it into the scenario with the `auto-answer` pattern.

### Blind transfer: B transfers A → C

```yaml
name: blind-transfer
phones: [a, b, c]
patterns:
  - {use: auto-answer, phone_id: b, delay_ms: 200}
  - {use: auto-answer, phone_id: c, delay_ms: 200}
  - {use: blind-transfer, phone_id: b,
     transfer_to: "sip:003@sip.example.com", after_ms: 2000}
  - {use: hangup-after-duration, phone_id: a, duration_ms: 5000}
  - {use: make-call-and-wait-confirmed, phone_id: a,
     dest_uri: "sip:002@sip.example.com"}
stop_on: [{phone_id: c, event: call.state.disconnected}]
timeout_ms: 12000
```

Atomic-tool equivalent: see `b_blind_transfer(dest_uri=...)`.

### Attended transfer: B holds A, consults C, bridges A ↔ C

The flow has enough steps that it's clearer as **inline scenario hooks**
than as a composite pattern:

```yaml
name: attended-transfer
phones: [a, b, c]
patterns:
  - {use: auto-answer, phone_id: b, delay_ms: 200}
  - {use: auto-answer, phone_id: c, delay_ms: 200}
  - {use: make-call-and-wait-confirmed, phone_id: a,
     dest_uri: "sip:002@sip.example.com"}
hooks:
  - when: call.state.confirmed
    on_phone: a
    once: true
    then:
      - wait: 1000ms
      - hold
      - make_call: {phone_id: a, to: "sip:003@sip.example.com"}
      - wait: 2500ms
      - attended_transfer
stop_on: [{phone_id: c, event: call.state.disconnected}]
timeout_ms: 15000
```

Both legs must belong to the same phone — cross-phone attended transfer
returns an error with a clear message.

### 3-way conference

```yaml
name: conference
phones: [a, b, c]
patterns:
  - {use: auto-answer, phone_id: b, delay_ms: 100}
  - {use: auto-answer, phone_id: c, delay_ms: 100}
initial_actions:
  - {action: make_call, phone_id: a, to: "sip:002@sip.example.com"}
  - {action: make_call, phone_id: a, to: "sip:003@sip.example.com"}
hooks:
  - when: call.state.confirmed
    on_phone: a
    once: true
    then:
      - wait: 2500ms
      - action: conference
        phone_id: a
        call_ids: auto        # engine resolves to all active calls on a
      - wait: 5000ms
      - hangup_all: {phone_id: a}
stop_on: [{phone_id: a, event: call.state.disconnected}]
timeout_ms: 20000
```

### Codec selection & mid-call change

Endpoint priority from YAML profile (`defaults.codecs: [PCMA]`); mid-call
re-INVITE via the `reinvite-codec-change` pattern:

```yaml
patterns:
  - {use: reinvite-codec-change, phone_id: a, new_codec: G722,
     trigger_at_ms: 3000}
  - {use: hangup-after-duration, phone_id: a, duration_ms: 5000}
  - {use: make-call-and-wait-confirmed, phone_id: a,
     dest_uri: "sip:002@sip.example.com"}
```

### SIP messaging

```yaml
initial_actions:
  - {action: send_message, phone_id: a,
     to: "sip:002@sip.example.com", body: "Hello!"}
stop_on: [{event: im.received, phone_id: b}]
timeout_ms: 2000
```

### Monitoring (always atomic — read-only introspection)

```
list_phones()                # reg state + active-call counts
a_get_active_calls()         # a's active calls with RTP
a_list_calls()               # compact summary incl. DISCONNECTED
get_sip_log(phone_id="a", last_n=30)
```

## Scenario Engine

**Goal.** Let an LLM write a multi-step SIP flow once and have it execute
deterministically. The engine replaces "call tool, wait 2 s, call next
tool" loops (which burn wall-clock on LLM-turn latency and race against
real SIP timers) with a YAML flow that runs in one asyncio loop.

### Three tools in a typical workflow

```python
get_scenario_template()                # YAML skeleton + event/action reference
validate_scenario(scenario=<dict>)     # static dry-run (no pjsua touched)
run_scenario(scenario=<dict>)          # execute and return the timeline
```

All three tools accept a scenario only as a Python dict — file paths are
not supported (the agent runs outside the container, so paths don't translate).

Scenarios are authored as **inline `hooks:`** — `when: <event>` + `then: [<actions>]`.
See the `pjsua-scenarios` skill for the canonical shape, the coordinator-hook
idiom, and worked examples (blind transfer, attended transfer).

### Event taxonomy

Hooks listen on:
- **Call state**: `call.state.{calling,incoming,early,connecting,confirmed,disconnected}`
- **DTMF**: `dtmf.in`, `dtmf.out`
- **Registration**: `reg.{started,success,failed,unregistered}`
- **Messaging**: `im.received`
- **Scenario lifecycle**: `scenario.{started,stopped}`
- **User-emitted**: `user.<name>` (from the `emit` action)

### Action vocabulary (19 actions)

- **Call control**: `answer`, `hangup`, `hangup_all`, `reject`, `hold`,
  `unhold`, `send_dtmf`, `blind_transfer`, `attended_transfer`,
  `conference`, `make_call`
- **Media**: `play_audio`, `stop_audio`, `send_message`, `set_codecs`
- **Flow control**: `wait`, `wait_until`, `emit`, `checkpoint`, `log`

Defaults inherited at dispatch time: `phone_id` from hook's `on_phone` or
triggering event, `call_id` from the triggering event.

### `stop_on` filters

```yaml
stop_on:
  - phone_id: a
    event: call.state.disconnected
    call_id: 2                       # specific call-id
  - event: call.state.disconnected
    match: {last_status: "4xx"}      # predicate — supports exact, list,
                                     # "4xx"/"5xx", "~regex"
```

### Pre-flight validation

`run_scenario` auto-runs `validate_scenario` first. Typos (wrong action,
wrong event prefix, malformed hooks) return `status="error"` in <100 ms —
no wall-clock burned on the timeout.

### Worked examples

Examples live in the `pjsua-scenarios` skill (shipped alongside the MCP):

- `.claude/skills/pjsua-scenarios/SKILL.md` — anatomy, idioms, simple-call template
- `.claude/skills/pjsua-scenarios/examples/blind-transfer.md` — REFER + sequencing
- `.claude/skills/pjsua-scenarios/examples/attended-transfer.md` — coordinator hook + REFER/Replaces

## Call Info & RTP Statistics

`<phone>_get_call_info` returns live call data, including RTCP-derived RTP stats:

```json
{
  "phone_id": "a",
  "call_id": 0,
  "state": "CONFIRMED",
  "remote_uri": "sip:002@sip.example.com",
  "remote_contact": "<sip:192.0.2.10:5060;ob>",
  "local_contact": "<sip:1001@192.0.2.20:5062>",
  "codec": "PCMA",
  "duration": 45,
  "recording_file": "/recordings/a/call_0_20260101_141603_528491.wav",
  "playing_file": "/app/audio/moh.wav",
  "rtp": {
    "tx_packets": 2250, "tx_bytes": 360000,
    "rx_packets": 2248, "rx_bytes": 359680,
    "rx_loss": 0, "rx_dup": 0, "rx_reorder": 0, "rx_discard": 0,
    "rx_jitter_usec": 875, "rtt_usec": 6362
  }
}
```

`<phone>_get_active_calls` returns this for every active call on the phone at once — no need to iterate `call_id`s.

## Call Recording (per-phone toggle, paired pcap)

Recording is **off by default** — opt in per phone with
`recording_enabled: true` in YAML or `recording_enabled=True` in
`add_phone`. When enabled, every call on the phone is written to the
container path `/recordings/<phone_id>/` as two paired files:

```
/recordings/
├── a/
│   ├── call_0_20260422_145828_123456.wav        # local + remote audio mixed
│   └── call_0_20260422_145828_123456.meta.json  # context sidecar
├── b/
│   └── ...
```

The filename carries a microsecond suffix so a single call can produce
several WAVs if recording is toggled mid-call (see below). The sidecar
carries the context the WAV itself lacks:

```json
{
  "phone_id": "a", "call_id": 0, "direction": "outbound",
  "started_at": "2026-04-22T14:58:28+00:00",
  "ended_at":   "2026-04-22T14:58:54+00:00",
  "duration": 26, "codec": "PCMA",
  "remote_uri": "sip:123002@...", "last_status": 200, "last_status_text": "OK",
  "recording": "/recordings/a/call_0_20260422_145828_123456.wav",
  "pcap":      "/captures/a/call_0_20260422_145828.pcap"
}
```

`pcap` is populated whenever a capture was running for the phone during
the call — either a manual `start_capture(phone_id="a")` or (more
commonly) the per-phone auto-capture driven by `capture_enabled`. In
both cases the pcap lives under `/captures/<phone_id>/` with the same
basename as the recording, so audio and signalling pair up without any
timestamp matching.

### Per-phone toggle: `recording_enabled`

Each phone carries a `recording_enabled` flag (default `false`). Set it
up-front in YAML or at runtime — toggles take effect instantly on every
active call of that phone:

```yaml
# config/phones.yaml
defaults:
  domain: sip.example.com
  password: xxx
  # recording_enabled: false      # default — nobody records
phones:
  - phone_id: a
    username: "1001"
    recording_enabled: true       # per-phone opt-in
  - phone_id: b
    username: "1002"              # stays off
```

```
add_phone(phone_id="c", domain="...", username="1003", password="x",
          recording_enabled=True)                         # opt-in at add time

update_phone(phone_id="a", recording_enabled=True)        # flip on mid-call
update_phone(phone_id="a", recording_enabled=False)       # flip back off
```

Every `off → on` opens a fresh WAV with a new microsecond-unique filename
and every `on → off` closes the current WAV and writes its `.meta.json`
sidecar. So `on → off → on → off → on → hangup` produces **three**
WAV+sidecar pairs under `/recordings/<phone_id>/`, not one. Use
`list_recordings(phone_id=..., call_id=...)` to see every segment for
a given call; `<phone>_get_recording(call_id=...)` returns only the
currently-open segment.

**To hide recording files from the host entirely**, drop the
`./recordings` bind mount from your `docker-compose.yml` — `/recordings`
will live inside the ephemeral container FS and disappear with `--rm`.

**Music-on-Hold** plays automatically when a call connects — Suite Espanola Op. 47 — Leyenda (Albeniz), CC0 public domain from FreeSWITCH/MUSOPEN, 8kHz WAV. Use `<phone>_play_audio` to override, `<phone>_stop_audio` to resume MOH.

## SIP Log Inspection

Every SIP message the PJSUA2 stack processes is captured by a custom `LogWriter` into a bounded in-memory deque (5000 entries):

```
get_sip_log()                                        # everything (all phones)
get_sip_log(last_n=20)
get_sip_log(filter_text="REGISTER")
get_sip_log(filter_text="401")
get_sip_log(phone_id="a")                            # substring filter on `sip:<a's user>@`
get_sip_log(phone_id="a", filter_text="INVITE")      # composable
```

Each entry contains:
- `level` — pjsip log level (1=error … 5=trace)
- `msg` — full log line, including SIP message dumps
- `thread` — originating pjlib thread name

## Packet Capture

Two independent modes coexist: **manual** (one-shot tcpdump you fire
from a tool call) and **auto-capture** (per-phone `capture_enabled`
flag — tcpdump opens on the first audio-active call and closes on the
last disconnect). Both land under `/captures/<phone_id>/` with the same
basename as the recording, so pcap and WAV pair up on disk.

### Per-phone auto-capture (`capture_enabled`)

Default is `false` — no tcpdump runs unless you opt in. Turn it on in
YAML or at runtime; the state is checked per call, so you can flip it
mid-session:

```yaml
# config/phones.yaml
phones:
  - phone_id: a
    username: "1001"
    capture_enabled: true        # every call on 'a' → pcap
  - phone_id: b
    username: "1002"             # inherits default → no pcap
```

```
add_phone(phone_id="c", domain="...", username="1003", password="x",
          capture_enabled=True)                         # opt-in at add time
update_phone(phone_id="a", capture_enabled=False)       # flip off mid-call
update_phone(phone_id="a", capture_enabled=True)        # flip back on
```

On→off during a live call flushes and closes the current pcap; off→on
opens a fresh pcap with a new microsecond-unique filename. Off→on **does
not** retroactively capture packets from earlier in the call.

Each auto-capture uses the broad BPF filter `udp`, so a re-INVITE that
changes the RTP port (hold/unhold, codec swap) does not drop any packets
mid-call. The tradeoff is disk: on a noisy network the pcap grows faster
than if we locked to a single port. If you need to trim, split the pcap
post-hoc — see below.

In a conference (two active calls on one phone) a **single** pcap is
kept for the phone, not one per leg. The first call starts it; the last
disconnect closes it.

### Manual capture

Fire tcpdump on demand without the per-phone flag:

```
start_capture()                       # host-wide → /captures/capture_<ts>.pcap
start_capture(phone_id="a")           # BPF: udp port <a's local port>
stop_capture()                        # stops the manual capture
stop_capture(phone_id="a")            # stops phone 'a's auto-capture
get_pcap()                            # file info (most recent)
```

If `start_capture(phone_id="a")` is called while phone `a` already has
auto-capture running, the call is refused with an explanatory error —
disable `capture_enabled` first (or stop the auto-capture explicitly).

### Splitting SIP and RTP after the fact

Because the BPF filter is broad (`udp`), the pcap contains both SIP
signalling and RTP media interleaved. Split with `tshark` post-hoc:

```bash
tshark -Y 'sip' -r captures/a/call_0_*.pcap -w sip_only.pcap
tshark -Y 'rtp' -r captures/a/call_0_*.pcap -w rtp_only.pcap
```

## Dynamic Tool Registration

`load_phones` / `add_phone` / `drop_phone` call `mcp.add_tool()` and `mcp.remove_tool()` at runtime. The MCP server announces the change via `notifications/tools/list_changed`; compatible clients rescan the tool list immediately.

```
# Fresh server
list_tools()                → 14 static tools
add_phone("alice", ...)     → 14 + 22 = 36 tools (alice_make_call, alice_hangup, ...)
add_phone("bob", ...)       → 14 + 22·2 = 58 tools
drop_phone("alice")         → 14 + 22 = 36 tools
```

The `tools_changed=True` capability is opt-in in the MCP protocol; the server enables it via a `create_initialization_options` monkey-patch on startup.

## Testing

### Unit tests

```bash
docker compose run --rm --entrypoint pytest pjsua-mcp tests/ -m "not integration" -v
```

Covers SipEngine lifecycle, PhoneRegistry CRUD + two-account isolation, CallManager lookups, PcapManager, SipLogWriter, plus the full **scenario engine** suite: EventBus pub/sub + threading, HookRuntime match semantics, every wired action via MockCallManager, TimelineRecorder offsets, pre-flight validator covering every action/event prefix. Fast (~7 s, ~125 tests), no network.

### Integration tests (self-contained)

```bash
docker compose -f docker-compose.test.yml run --build --rm test-runner
```

Runs one MCP server subprocess per test class + an Asterisk PBX container on an isolated Docker network (ext 6001/6002/6003). Exercises registration, outbound/inbound calls, blind + attended transfer, conference, codec negotiation, SIP MESSAGE, reject, history, YAML profile loading (replace vs merge), dynamic tool add/remove, cross-phone attended-transfer rejection, per-phone recording layout with paired pcap and `.meta.json` sidecar.

The full suite runs in ~2 minutes (~90 tests).

```
┌──────────────────────────────────────────────────────────┐
│  Docker Compose network: sipnet                          │
│                                                          │
│  ┌──────────────────────────────────────────────────────┐│
│  │  test-runner container                               ││
│  │                                                      ││
│  │  pytest spawns ONE MCP server subprocess per test    ││
│  │  class. That server adds several phones via          ││
│  │  add_phone / load_phones and drives them:     ││
│  │                                                      ││
│  │     ┌──────────────────────────────────────┐         ││
│  │     │  MCP Server (a, b, c managed inside) │         ││
│  │     └──────────────┬───────────────────────┘         ││
│  │                    │ SIP/UDP                         ││
│  │                    ▼                                 ││
│  │          ┌──────────────────────┐                    ││
│  │          │  Asterisk PBX         │                   ││
│  │          │  ext 6001/6002/6003   │                   ││
│  │          └──────────────────────┘                    ││
│  └──────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────┘
```

## Publishing to Harbor (or any OCI registry)

This repo ships only the image artifact. Distribution to clients (wrapper scripts, slash-commands, MCP config) belongs to a separate **plugin repo** that references the published image by tag.

### One-time setup

1. Put registry coordinates in `.env` (gitignored; see `.env.example`):
   ```
   HARBOR_HOST=harbor.example.corp
   HARBOR_PROJECT=voip-tools
   HARBOR_IMAGE=pjsua-mcp
   ```
2. Cache credentials once: `docker login "$HARBOR_HOST"` — they live in `~/.docker/config.json`.

### Publish a release (manual)

```bash
./scripts/publish.sh v0.3.0                   # builds, tags :v0.3.0 + :latest, pushes both
./scripts/publish.sh v0.3.0-rc1 --no-latest   # pre-release — keep :latest pointing at stable
./scripts/publish.sh v0.3.0 --platform linux/amd64,linux/arm64  # multi-arch via buildx
```

The script is read-only until `docker push` runs — safe to dry-run manually. `.dockerignore` keeps the build context small (excludes `captures/`, `recordings/`, `config/phones.yaml`, `.env`, CI files), so nothing secret or bulky gets shipped into image layers.

### How clients consume it

In your plugin-repo's wrapper script:

```bash
IMAGE="${PJSUA_MCP_IMAGE:-harbor.example.corp/voip-tools/pjsua-mcp:v0.3.0}"
exec docker run -i --rm \
  --network host \
  --cap-add NET_RAW --cap-add NET_ADMIN \
  --user "$(id -u):$(id -g)" \
  -v "$CONFIG_DIR:/config:ro" \
  -v "$DATA_DIR/captures:/captures" \
  -v "$DATA_DIR/recordings:/recordings" \
  "$IMAGE"
```

Pin a **specific semver tag** in the plugin — never `:latest` for production clients — so a breaking image change doesn't silently land on every user's machine.

## Project Structure

```
pjsua_mcp/
├── src/
│   ├── server.py                    # MCP entry point, 19 static tool definitions, lifespan
│   ├── sip_engine.py                # Endpoint lifecycle, per-phone transport create/close, codecs
│   ├── account_manager.py           # PhoneRegistry, PhoneConfig, SipAccount (emits reg.* / im.* events)
│   ├── call_manager.py              # SipCall, per-phone queues, incoming routing (emits call.state.* / dtmf.in)
│   ├── phone_tool_factory.py        # 22 closures × N phones; add_tool / remove_tool
│   ├── sip_logger.py                # Custom LogWriter → bounded deque
│   ├── pcap_manager.py              # tcpdump subprocess management
│   └── scenario_engine/             # Event-driven scenario runtime
│       ├── event_bus.py             # Thread-safe pub/sub; wildcard subscribe; wait_for
│       ├── hook_runtime.py          # Arm hooks, match events, dispatch actions
│       ├── action_executor.py       # 19 actions → CallManager / PhoneRegistry / SipEngine
│       ├── orchestrator.py          # ScenarioRunner — arms hooks, runs initial_actions, awaits stop_on
│       ├── timeline.py              # Chronological event+action recorder with ms offsets
│       └── validator.py             # Pre-flight static checker (typos, unknown actions/events)
├── config/
│   ├── phones.example.yaml          # YAML profile template (tracked)
│   └── .gitignore                   # ignores phones.yaml (real credentials stay out of git)
├── audio/
│   └── moh.wav                      # Default MOH — CC0, FreeSWITCH/MUSOPEN
├── tests/
│   ├── conftest.py
│   ├── test_sip_engine.py
│   ├── test_sip_logger.py
│   ├── test_account_manager.py         # legacy single-account API kept compatible
│   ├── test_phone_registry.py          # multi-phone registry + two-account isolation
│   ├── test_call_manager.py
│   ├── test_pcap_manager.py
│   ├── test_integration.py             # end-to-end against Asterisk
│   ├── scenario_engine/                # ~70 unit tests for the engine
│   │   ├── test_event_bus.py
│   │   ├── test_hook_runtime.py
│   │   ├── test_orchestrator.py
│   │   ├── test_timeline.py
│   │   ├── test_validator.py
│   │   └── test_actions_direct.py
│   └── asterisk/
│       ├── Dockerfile
│       ├── pjsip.conf
│       ├── extensions.conf
│       └── modules.conf
├── scripts/
│   └── publish.sh                   # Build + tag + push image to Harbor (manual one-liner)
├── Dockerfile                       # Multi-stage: build pjproject + runtime
├── .dockerignore                    # Trim build context (ignore recordings/captures/secrets)
├── docker-compose.yml               # Mounts ./config (ro), ./recordings, ./captures
├── docker-compose.test.yml          # Asterisk + test runner on sipnet
├── .env.example                     # UID/GID + HARBOR_HOST/HARBOR_PROJECT (copy to .env)
├── requirements.txt                 # mcp[cli], PyYAML, pydantic, pytest, jinja2, jsonschema
├── pyproject.toml
└── .mcp.json                        # MCP client config for AI assistants
```

## Technical Notes

- **Python 3.13 + pjproject 2.14.1** — built from source in a multi-stage Docker build. Python 3.13 removed `distutils`, so `setuptools` is installed before building the SWIG bindings.
- **Null audio device** — runs headless in Docker with no sound card. ALSA library is still linked at runtime.
- **One `pj.Endpoint`, N `pj.Account`** — pjsua2's native multi-account model. Each phone gets its own UDP transport (`ep.transportCreate`), so packet capture and SIP Contact ports stay distinct per phone.
- **Incoming call routing** — each `SipAccount`'s `onIncomingCall` callback is wired via a per-phone closure in `CallManager._make_incoming_handler`, so the call lands in the right phone's `_incoming_queue`.
- **Threading model** — `threadCnt=0` with manual event loop polling from the asyncio thread (~50 polls/sec). SWIG director callbacks (LogWriter) don't work reliably from executor threads.
- **stdout protection** — C-level fd 1 is redirected to stderr at startup. MCP JSON-RPC uses a saved copy of the original stdout fd. Prevents pjlib console output from corrupting the MCP channel.
- **SIP log** — `consoleLevel=5` (matching `level=5`) ensures the global log level isn't suppressed. The LogWriter captures everything into a thread-safe bounded deque.
- **Auto-answer** — deferred to the event poll loop (not inside `onIncomingCall`) to avoid PJSUA2 call state machine issues.
- **Recording** — per-phone `recording_enabled` flag (default off — opt in per phone). When on, writes to `/recordings/<phone_id>/call_<call_id>_<ts>_<us>.wav` plus a `.meta.json` sidecar with call context and the paired pcap path (when a capture is running for the phone). The recorder is connected AFTER player setup to avoid conference bridge disruption and reconnected on every `onCallMediaState`. Local + remote audio mixed into one mono WAV. Toggling `recording_enabled` mid-call via `update_phone` opens/closes distinct WAV segments — each with its own sidecar — so a single call can emit several recordings if the operator wants finer-grained capture.
- **Auto-capture** — per-phone `capture_enabled` flag (default off). Opens a dedicated `tcpdump -i any udp` subprocess on the first audio-active call and closes it on the last disconnect. Filter stays broad so re-INVITE RTP port changes don't drop packets; split SIP and RTP with `tshark -Y` after the fact. Start/stop requests come from pj callback threads; actual subprocess launches run on the asyncio poll loop via a deque-based pending queue (same pattern as `process_auto_answers`). Conference (2+ calls on one phone) shares a single pcap, counted via `_active_calls_by_phone`.
- **Re-INVITE** — audio player is reconnected to the new `aud_med` port after re-INVITE (codec change, conference conversion) so TX keeps flowing.
- **Dynamic tool registration** — `tools_changed=True` capability enabled via `create_initialization_options` monkey-patch; `ctx.session.send_tool_list_changed()` fires after each phone add/drop (once per batch for `load_phones`).
- **Stale call cleanup** — disconnected calls are removed from tracking; accounts are shut down before re-registration to prevent ghost sessions.
- **Single point of failure** — one container crash now drops all N phones. Acceptable for a dev/test stand. Docker-compose can `restart: unless-stopped` if you need resilience.
- **MOH** — Suite Espanola Op. 47 — Leyenda (Albeniz), classical guitar, CC0 public domain from FreeSWITCH/MUSOPEN.
