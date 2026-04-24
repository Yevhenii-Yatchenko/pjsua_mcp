# Agent Handoff — Running the pjsua Scenario Test Plan

You are the test-runner agent. Your job is to execute the 11 scenarios from
`docs/agent-test-plan.md` against a live Asterisk dev-stand, collect results,
and produce a report. You may also be asked to author new scenarios from
scratch — instructions for that below.

## Part 0 — Before you start

1. **Verify the MCP server is connected.** Look for `mcp__pjsua__*` tools
   (or plugin-prefixed equivalents) in your tool list. If missing, ask the
   user to start the stack:
   ```
   docker compose -f docker-compose.test.yml up -d
   ```
2. **Read the two canonical docs** (in this order):
   - `docs/scenarios-guide.md` — scenario format + inline hooks + stop_on filters
   - `scenarios/patterns/SCHEMA.md` — action vocabulary + event taxonomy
3. **Confirm pattern library is loaded:**
   ```
   list_patterns()   # expect 14 patterns returned
   ```
   If this returns an error, the MCP server isn't up.

## Part 1 — Provision phones (preamble, once)

Use three phones by convention — `a`, `b`, `c` — mapped to Asterisk
extensions `6001`, `6002`, `6003`:

```python
add_phone(phone_id="a", domain="asterisk", username="6001", password="test123",
          codecs=["PCMA"], recording_enabled=True, capture_enabled=True)
add_phone(phone_id="b", domain="asterisk", username="6002", password="test123",
          codecs=["PCMA"], recording_enabled=True)
add_phone(phone_id="c", domain="asterisk", username="6003", password="test123",
          codecs=["PCMA"], recording_enabled=True)

# Verify all three registered (wait up to 5s)
list_phones()  # expect all three with is_registered=true
```

If any phone isn't registered after 5 seconds, **do not proceed** — first
diagnose with `get_sip_log(phone_id="a", filter_text="REGISTER")`. Common
causes: wrong credentials, Asterisk not healthy, no IP route.

## Part 2 — Running each scenario

For each scenario in `docs/agent-test-plan.md` (there are 11), follow this
loop:

### 2a. Assemble the scenario

Either read the YAML straight from `scenarios/examples/` or build a dict
from the snippets in the test plan. Prefer the YAML files when available —
they are the authoritative version.

### 2b. Execute

```python
result = run_scenario(scenario="<dict or path>", timeout_ms=<from plan>)
```

### 2c. First pass-fail checks (do these for every scenario)

Before domain-specific assertions, verify basic health:

| Check | Pass |
|---|---|
| `result["status"] == "ok"` | ✅ progressed past stop_on |
| `result["status"] == "timeout"` | ❌ scenario hit timeout; check `result["timeline"]` to see where it got stuck |
| `result["status"] == "error"` | ❌ pattern load / action dispatch failed; check `result["errors"]` |
| `result["elapsed_ms"]` is within expected range for the scenario | — |
| `result["errors"]` is empty (or only `action.unknown` for gaps you already know about) | ✅ no silent failures |

### 2d. Domain-specific assertions

Use the **Pass iff** line from each scenario in `docs/agent-test-plan.md`.
Verify via timeline entries + read-only MCP tools:

```python
# Timeline inspection — is the key event present?
has_confirmed = any(
    e["type"] == "call.state.confirmed" and e["phone_id"] == "a"
    for e in result["timeline"]
)

# Post-run introspection
logs = get_sip_log(phone_id="a", last_n=30, filter_text="REFER")
recs = list_recordings(phone_id="a")
hist = a_get_call_history(last_n=1)
```

### 2e. Record the result

Keep a running tally. For each scenario write one row with:
- scenario name, status, elapsed, short note (especially on failures)
- on failure: attach the last 5-10 entries of `result["timeline"]` and any
  relevant `get_sip_log` output

## Part 3 — Debugging a failing scenario

If `status == "timeout"`:
1. Look at last 5 timeline entries — what was the last event/action?
2. Check if a hook's `when:` event type ever fired in the timeline.
3. Check if a hook's `match:` predicate was too strict (e.g. expected
   `status_code=200` but got `180`).
4. Consider timing: did an action's `wait:` complete before the dependent
   event? Add 500-1000 ms safety margin.

If `status == "error"`:
- `result["errors"]` has structured entries: `{stage, error, ...}`.
- `pattern.error` — typo in pattern name or bad params. Fix the YAML.
- `initial.error` — an action in initial_actions failed. Usually wrong
  phone_id or missing required arg.
- `hook_spec error` — hook `when:` is missing or malformed.

If `status == "ok"` but assertions fail:
- The engine ran the scenario, but Asterisk didn't behave as expected.
- Read `get_sip_log` for the relevant phone to see actual SIP exchange.
- Check Asterisk logs via `docker compose -f docker-compose.test.yml logs asterisk`.

## Part 4 — Test report format

After all 11 scenarios are run, produce a report:

```markdown
## Test run 2026-04-24 19:00

| # | Scenario | Status | Elapsed | Notes |
|---|---|---|---|---|
| 1 | hello-world | ✅ ok | 5.8s | — |
| 2 | reject-flow | ✅ ok | 1.2s | — |
| 3 | hold-unhold | ⚠️ flaky | 8.3s | 2nd re-INVITE missing 1 run of 3 |
| 4 | blind-transfer | ✅ ok | 4.1s | REFER + Refer-To observed |
| 5 | attended-transfer | ❌ fail | 12.0s | Replaces header missing — see timeline |
| 6 | conference | ❌ error | 2.1s | conference action: `get_active_calls` returned 1, expected 2 |
| 7 | codec-change | ✅ ok | 5.2s | G.722 re-INVITE confirmed in sip_log |
| 8 | dtmf-both-methods | ✅ ok | 3.1s | 4 dtmf.in events recorded |
| 9 | instant-message | ✅ ok | 0.5s | body round-tripped |
| 10 | register-cycle | ✅ ok | 2.0s | (not via run_scenario — direct MCP tools) |
| 11 | record-and-capture | ✅ ok | 4.8s | WAV and pcap paired correctly |

Tool coverage: 28/35 MCP tools exercised via scenarios.
Not exercised (manual-only): add_phone, drop_phone, load_phone_profile,
update_phone, start_capture, stop_capture, get_phone_profile_example.

Open issues discovered during run:
1. [scenario 5] `attended_transfer` action succeeded at engine level but
   Asterisk did not emit Replaces — likely Asterisk config issue, not engine.
2. [scenario 6] `conference: {call_ids: "auto"}` resolves to only 1 call;
   engine's `get_active_calls` might miss the second leg if it isn't yet
   in CONFIRMED state when the hook fires. Workaround: add 1-2s wait
   before the conference action.
```

## Part 5 — Writing a NEW scenario from scratch

If the user asks you to author a new scenario (not on the 11-list), follow
this recipe:

### Step 1 — Natural-language outline

Describe the flow in plain text:
> "A calls B, B answers after 300ms. Both talk 3s. A puts B on hold,
> resumes 2s later. Both hang up."

### Step 2 — Identify events to hook on

Match each step in the narrative to an **event type** from the taxonomy:
- "B answers" → `call.state.confirmed` on b
- "3s pass" → timer inside a hook's `then:`
- "A puts on hold" → action inside a hook fired on that timer

### Step 3 — Decide patterns vs inline

For each reaction:
- If a pattern already captures it (check `list_patterns(tags=[...])`) —
  compose it: `{use: auto-answer, phone_id: b, delay_ms: 300}`
- If not — write an inline hook directly in the scenario's `hooks:` list

### Step 4 — Assemble the YAML

Minimum skeleton:
```yaml
name: <scenario-name>
phones: [a, b]
patterns:
  - {use: wait-for-registration, phone_id: a}
  - {use: wait-for-registration, phone_id: b}
  - {use: auto-answer, phone_id: b, delay_ms: 300}
  - {use: make-call-and-wait-confirmed, phone_id: a, dest_uri: "sip:6002@asterisk"}
initial_actions: []     # usually empty if a pattern has one
hooks:
  - when: call.state.confirmed
    on_phone: a
    once: true
    then:
      - wait: 3000ms
      - hold
      - wait: 2000ms
      - unhold
      - wait: 500ms
      - hangup
stop_on:
  - {phone_id: a, event: call.state.disconnected}
timeout_ms: 15000
```

### Step 5 — Validate by running

```python
result = run_scenario(scenario=<dict>)
print(result["status"], result["elapsed_ms"])
# Dump timeline if it didn't work
for e in result["timeline"]:
    print(e)
```

### Step 6 — Iterate

Common fix patterns:
- `status == "timeout"` — usually a hook didn't fire. Add `log` actions
  inside each hook so the timeline clearly shows which fired.
- Hook fired but wrong call_id — add `call_id` filter in `stop_on` or use
  `match:` predicate.
- Timing off — bump `wait:` values 20-50%.

## Part 6 — Events and actions — quick reference

### Events you can listen on (`when:`)

- `reg.{started,success,failed,unregistered}`
- `call.state.{calling,incoming,early,connecting,confirmed,disconnected}`
- `dtmf.{in,out}`
- `im.received`
- `scenario.{started,stopped}`
- `user.<name>` — anything you `emit`

### Actions you can put in `then:`

- **Call control:** `answer`, `hangup`, `hangup_all`, `reject`, `hold`,
  `unhold`, `send_dtmf`, `blind_transfer`, `attended_transfer`,
  `conference`, `make_call`
- **Media:** `play_audio`, `stop_audio`, `send_message`, `set_codecs`
- **Flow control:** `wait`, `wait_until`, `emit`, `checkpoint`, `log`

### Pre-flight validation

Before `run_scenario` touches pjsua, it runs `validate_scenario` internally.
Typos (bad pattern name, bad action, bad event type, missing required
params) return `status="error"` in <100ms — you DO NOT need to wait for
the scenario timeout. The report includes a list of structured `issues`
pointing at the exact offending field. Fix and re-run.

### Filters on `stop_on`

```yaml
stop_on:
  - phone_id: a
    event: call.state.disconnected
    call_id: 2                         # specific call only
    match: {last_status: "4xx"}        # predicate on event.data
```

## Part 7 — Actions NOT yet wired into scenarios

If a scenario needs one of these, call the MCP tool directly BEFORE or
AFTER `run_scenario` (not from inside):

- `register`, `unregister` — per-phone tools (`a_register()`, etc.)
- `update_phone` — toggle recording/capture mid-run
- `start_capture`, `stop_capture` — explicit pcap control

Example for register-cycle test (scenario #10 in test plan):
```python
a_unregister()
# Wait for reg.unregistered — can poll a_get_registration_status()
a_register()
# Wait for reg.success
```

## Part 8 — Quick sanity commands (keep handy)

```python
list_patterns()                                # what's in the library
get_pattern("auto-answer")                     # full spec + rendered body
list_phones()                                  # all phones + reg status
a_get_active_calls()                           # live call state
get_sip_log(phone_id="a", last_n=20)           # recent SIP trace
list_recordings(phone_id="a")                  # WAVs produced
```
