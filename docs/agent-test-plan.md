# Agent Test Plan — pjsua MCP via Scenarios

**Goal.** Validate that every meaningful MCP tool in the pjsua server can be
exercised *through the scenario engine* (not through manual orchestration).
One scenario per capability. Agent runs them in order, collects timelines +
read-only tool outputs, and reports pass/fail.

## Preconditions

1. Asterisk + pjsua-mcp containers up:
   ```
   docker compose -f docker-compose.test.yml up -d asterisk
   docker compose -f docker-compose.test.yml up -d   # (or MCP connected from host)
   ```
2. Asterisk provides extensions 6001/6002/6003 with password `test123`.
3. The agent has the MCP server connected (`mcp__pjsua__*` tools visible).

## Standard provisioning preamble (run once before ALL scenarios)

```python
# Provision three phones
load_phone_profile(path="/config/phones.test.yaml")
# or explicit:
add_phone(phone_id="a", domain="asterisk", username="6001", password="test123",
          codecs=["PCMA"], recording_enabled=True, capture_enabled=True)
add_phone(phone_id="b", domain="asterisk", username="6002", password="test123",
          codecs=["PCMA"], auto_answer=False, recording_enabled=True)
add_phone(phone_id="c", domain="asterisk", username="6003", password="test123",
          codecs=["PCMA"], auto_answer=False, recording_enabled=True)

# Sanity — all three registered?
list_phones()                          # expect 3 phones, is_registered=true each
```

## Test matrix — 11 scenarios

Each row: **trigger tool(s)** exercised via `run_scenario`, plus
**verification** via read-only MCP tools.

| # | Scenario | Triggers | Verifies |
|---|---|---|---|
| 1 | `hello-world` | make_call, answer, send_dtmf, hangup, reg.* | call.state.confirmed both sides, dtmf.in on B, call.state.disconnected on A |
| 2 | `reject-flow` | make_call, reject (486) | call.state.disconnected on A with `last_status=486` |
| 3 | `hold-unhold` | hold, unhold | sip_log shows 2 re-INVITEs (sendonly → sendrecv); RTP resumes |
| 4 | `blind-transfer` | blind_transfer (REFER) | sip_log shows REFER from B to A, new INVITE from A to C |
| 5 | `attended-transfer` | attended_transfer (REFER/Replaces) | sip_log shows REFER with `Replaces:` header |
| 6 | `conference` | conference (local bridge) | RTP flow active on BOTH legs on phone A simultaneously |
| 7 | `codec-change` | set_codecs mid-call | sip_log shows re-INVITE with new SDP codec; new codec active |
| 8 | `dtmf-both-methods` | send_dtmf (RFC2833 + SIP-INFO) | dtmf.in events arrive on callee, method field reflects source |
| 9 | `instant-message` | send_message, get_messages | B's get_messages has entry with expected body |
| 10 | `register-cycle` | unregister, register | reg.unregistered then reg.success events in timeline |
| 11 | `record-and-capture` | play_audio + recording_enabled + capture_enabled | list_recordings returns WAV; get_pcap returns pcap file paired with WAV |

## Per-scenario runbooks

### 1. hello-world

```python
run_scenario(scenario={
    "name": "hello-world",
    "phones": ["a", "b"],
    "patterns": [
        {"use": "wait-for-registration", "phone_id": "a"},
        {"use": "wait-for-registration", "phone_id": "b"},
        {"use": "auto-answer", "phone_id": "b", "delay_ms": 500},
        {"use": "send-dtmf-on-confirmed", "phone_id": "a", "digits": "1"},
        {"use": "hangup-after-duration", "phone_id": "a", "duration_ms": 5000},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk"},
    ],
    "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    "timeout_ms": 20000,
})
```
**Pass iff:**
- `result.status == "ok"`
- timeline contains `call.state.incoming` on b, `call.state.confirmed` on a
- `action.send_dtmf` with digits="1" recorded
- `action.answer` recorded after ~500ms from incoming
- `call.state.disconnected` on a at ~5000ms after confirmed

Post-run verify:
```python
a_get_call_history(last_n=1)           # duration ≈ 5s
list_recordings(phone_id="a")          # has a WAV file
get_sip_log(last_n=20, phone_id="a")   # shows INVITE, 200, ACK, BYE
```

### 2. reject-flow

```python
run_scenario(scenario={
    "name": "reject-flow",
    "phones": ["a", "b"],
    "patterns": [
        {"use": "reject-on-incoming", "phone_id": "b", "status_code": 486},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk", "timeout_ms": 3000},
    ],
    "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    "timeout_ms": 5000,
})
```
**Pass iff:** disconnect event on `a` has `last_status: 486`.

### 3. hold-unhold

```python
run_scenario(scenario={
    "name": "hold-unhold",
    "phones": ["a", "b"],
    "patterns": [
        {"use": "auto-answer", "phone_id": "b", "delay_ms": 100},
        {"use": "hold-and-resume", "phone_id": "a",
         "hold_start_delay_ms": 1000, "hold_duration_ms": 2000},
        {"use": "hangup-after-duration", "phone_id": "a", "duration_ms": 5000},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk"},
    ],
    "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    "timeout_ms": 15000,
})
```
**Pass iff:** timeline has `action.hold` then `action.unhold`. Post-check:
```python
get_sip_log(filter_text="a=sendonly", phone_id="a")  # at least 1 entry
get_sip_log(filter_text="a=sendrecv", phone_id="a")  # at least 2 entries
```

### 4. blind-transfer

```python
run_scenario(scenario={
    "name": "blind-transfer",
    "phones": ["a", "b", "c"],
    "patterns": [
        {"use": "auto-answer", "phone_id": "b", "delay_ms": 200},
        {"use": "auto-answer", "phone_id": "c", "delay_ms": 200},
        {"use": "blind-transfer", "phone_id": "b",
         "transfer_to": "sip:6003@asterisk", "after_ms": 1500},
        {"use": "hangup-after-duration", "phone_id": "a", "duration_ms": 4000},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk"},
    ],
    # stop_on matches on C's disconnect — A's first disconnect (old leg) is
    # ignored because the predicate pins it to phone c.
    "stop_on": [{"phone_id": "c", "event": "call.state.disconnected"}],
    "timeout_ms": 12000,
})
```
**Pass iff:** sip_log shows REFER from B's side → INVITE to C from A's side.
```python
get_sip_log(filter_text="REFER")         # expect at least 1
get_sip_log(filter_text="sip:6003")      # expect INVITE + 200 OK
```

### 5. attended-transfer

```python
run_scenario(scenario={
    "name": "attended-transfer",
    "phones": ["a", "b", "c"],
    "patterns": [
        {"use": "auto-answer", "phone_id": "b", "delay_ms": 200},
        {"use": "auto-answer", "phone_id": "c", "delay_ms": 200},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk"},
    ],
    # Inline hooks (once Scenario.hooks is wired; else add an ad-hoc pattern):
    "hooks": [
        {"when": "call.state.confirmed", "on_phone": "a", "once": True,
         "then": [
             {"wait": "1000ms"},
             "hold",
             {"make_call": {"phone_id": "a", "to": "sip:6003@asterisk"}},
             {"wait": "2500ms"},
             "attended_transfer",
         ]},
    ],
    "stop_on": [{"event": "call.state.disconnected", "phone_id": "a"}],
    "timeout_ms": 15000,
})
```
**Pass iff:** sip_log shows REFER with `Replaces:` header:
```python
get_sip_log(filter_text="Replaces:")   # at least 1 entry
```

### 6. conference (local bridge)

```python
run_scenario(scenario={
    "name": "conference",
    "phones": ["a", "b", "c"],
    "patterns": [
        {"use": "auto-answer", "phone_id": "b", "delay_ms": 100},
        {"use": "auto-answer", "phone_id": "c", "delay_ms": 100},
    ],
    "initial_actions": [
        {"action": "make_call", "phone_id": "a", "to": "sip:6002@asterisk"},
        {"action": "make_call", "phone_id": "a", "to": "sip:6003@asterisk"},
    ],
    "hooks": [
        {"when": "call.state.confirmed", "on_phone": "a", "once": True,
         "then": [
             {"wait": "2500ms"},
             {"action": "conference", "phone_id": "a", "call_ids": "auto"},
         ]},
    ],
    "stop_on": [{"phone_id": "a", "event": "scenario.timeout"}],
    "timeout_ms": 8000,
})
```
**Pass iff:** `a_get_active_calls` returns 2 entries, both with non-zero RTP bytes.

### 7. codec-change (re-INVITE mid-call)

```python
# Initial codecs are set per-phone in add_phone call above.
run_scenario(scenario={
    "name": "codec-change",
    "phones": ["a", "b"],
    "patterns": [
        {"use": "auto-answer", "phone_id": "b"},
        {"use": "reinvite-codec-change", "phone_id": "a",
         "new_codec": "G722", "trigger_at_ms": 2000},
        {"use": "hangup-after-duration", "phone_id": "a", "duration_ms": 5000},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk"},
    ],
    "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    "timeout_ms": 10000,
})
```
**Pass iff:** sip_log shows 2nd INVITE (re-INVITE) with `m=audio ... RTP/AVP 9` (G.722 payload type 9).

### 8. dtmf-both-methods

Exercise RFC2833 (default) and SIP-INFO fallback. Pjsua decides method; just
verify digits arrive.

```python
run_scenario(scenario={
    "name": "dtmf-smoke",
    "phones": ["a", "b"],
    "patterns": [
        {"use": "auto-answer", "phone_id": "b"},
        {"use": "send-dtmf-on-confirmed", "phone_id": "a", "digits": "123#"},
        {"use": "hangup-after-duration", "phone_id": "a", "duration_ms": 3000},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk"},
    ],
    "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    "timeout_ms": 8000,
})
```
**Pass iff:** `b_get_call_info` or timeline shows 4 dtmf.in events with digits 1,2,3,#.

### 9. instant-message

```python
run_scenario(scenario={
    "name": "im-smoke",
    "phones": ["a", "b"],
    "patterns": [],
    "initial_actions": [
        {"action": "send_message", "phone_id": "a",
         "to": "sip:6002@asterisk", "body": "hello from a"},
    ],
    "stop_on": [],
    "timeout_ms": 2000,
})
```
**Pass iff:**
```python
b_get_messages(last_n=1)           # one entry, body=="hello from a"
```

### 10. register-cycle

```python
# This scenario does NOT use patterns — it calls MCP tools directly
# (register / unregister are scenario-setup, not yet wired as actions).
a_unregister()
# expect reg.unregistered event — check via pattern that listens?
# Easier: directly poll a_get_registration_status()
a_register()
# Expect reg.success within a few seconds.
```
**Pass iff:** after unregister: is_registered=False. After register:
is_registered=True, status_code=200.

### 11. record-and-capture

```python
# Phones provisioned with recording_enabled=True, capture_enabled=True
run_scenario(scenario={
    "name": "capture-smoke",
    "phones": ["a", "b"],
    "patterns": [
        {"use": "auto-answer", "phone_id": "b"},
        {"use": "hangup-after-duration", "phone_id": "a", "duration_ms": 4000},
        {"use": "make-call-and-wait-confirmed", "phone_id": "a",
         "dest_uri": "sip:6002@asterisk"},
    ],
    "stop_on": [{"phone_id": "a", "event": "call.state.disconnected"}],
    "timeout_ms": 10000,
})
```
**Pass iff:**
```python
list_recordings(phone_id="a")     # ≥1 WAV, size > 0
get_pcap()                         # most recent pcap exists, paired with WAV basename
```

## Overall agent report template

```markdown
## Test run <YYYY-MM-DD HH:MM>

| # | Scenario | Status | Elapsed | Notes |
|---|---|---|---|---|
| 1 | hello-world | ✅ ok | 5.8s | all assertions passed |
| 2 | reject-flow | ✅ ok | 1.2s | 486 observed |
| 3 | hold-unhold | ⚠️ flaky | 8.3s | 2nd re-INVITE missing on 1 run of 3 |
| … | … | … | … | … |

Tool coverage: 28/35 MCP tools exercised (`add_phone`, `drop_phone`, … ✅ /
`start_capture`, `stop_capture`, `update_phone`, `get_phone_profile_example`
not tested — scenario-setup tools; covered manually).

Open issues discovered:
- stop_on doesn't support call_id filter (scenario #4 early-terminate)
- conference action needs auto-fill of call_ids (scenario #6)
- register/unregister not wired as actions (scenario #10 uses direct tool)
```

## How the agent iterates

1. Run scenario #1. If pass, move to #2. If fail, capture the timeline and
   relevant read-only outputs into a diagnostic block.
2. Don't retry flaky scenarios more than 3× — mark as ⚠️ with evidence.
3. After all scenarios, output the report table + list of open issues.
4. If a scenario exposed an engine bug (not a pjsua/Asterisk bug), open a
   follow-up task with a minimal reproducer.

## Tools NOT covered by scenarios

Call-setup tools that run *outside* scenarios:

- `add_phone`, `drop_phone`, `load_phone_profile`, `get_phone_profile_example`
  — lifecycle before/after scenarios
- `update_phone` — dynamic toggle; can be made into an action if needed
- `list_phones`, `get_phone` — read-only diagnostic (not exercised via scenario
  but used in preamble)
- `start_capture`, `stop_capture` — most phones use `capture_enabled` profile
  toggle; explicit start/stop for niche cases

These should still be tested manually once — just not through `run_scenario`.
