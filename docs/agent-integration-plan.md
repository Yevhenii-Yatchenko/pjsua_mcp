# Integration Test Plan — Full MCP Surface Against PortaSIP Stand

You are a test-runner agent. Your job: verify **every** pjsua MCP tool on
the live PortaSIP stand at `192.168.1.202`, in both modes — classic
direct-tool calls (via the `sip-phones` skill idiom) and via the new
scenario engine — and produce a structured report.

**Total surface to verify:** 36 classic tools (22 per-phone templates
registered by `phone_tool_factory.py` × each provisioned phone + 14
static tools in `server.py`) + 5 scenario tools + 20 scenario actions +
16 event types.

---

## Preconditions

1. **Test stand reachable.** We run calls against the live PortaSIP stand
   (**not** the `docker-compose.test.yml` Asterisk — that container is for
   unit-level integration only). Stand credentials:
   - SIP proxy / domain: `192.168.1.202`
   - Password (all accounts): `zzzxxx123`
   - Accounts available: `123000`, `123001`, `123002`, `123003`,
     `123004`, `123005`, `123006`
   - Phone-role mapping for this plan:
     `a=123000`, `b=123001`, `c=123002`, `d=123003`, `e=123004`,
     `f=123005`, `g=123006`
   - Transport: UDP (change in `add_phone` if TLS/TCP is required by the
     stand's registrar policy)
2. **MCP server connected.** Verify `mcp__pjsua__*` tools are visible.
3. **Docs loaded into context.** Before touching the stand:
   - `docs/scenarios-guide.md`
   - `scenarios/patterns/SCHEMA.md`
   - `docs/agent-handoff.md` (debugging recipes, report format)
4. **Clean slate.** If any phone exists from a previous session, drop it
   via `drop_phone` before the preamble.
5. **URI substitution.** Example scenarios under `scenarios/examples/` hard-
   code Asterisk-style URIs (`sip:6002@asterisk`, `sip:6999@asterisk`) for
   CI. PortaSIP will **not** resolve `@asterisk`, so the filename form of
   `run_scenario` is unusable cross-stand. Two valid paths:
   - **Inline dict (preferred).** Load the YAML in the agent, parse to
     dict, substitute `@asterisk` → `@192.168.1.202` and the Asterisk
     extensions (`6002`, `6999`) → a stand-side ext from
     `123000..123006`, then `run_scenario(scenario=<dict>)`.
   - **Temp file copy.** Copy the YAML to `/tmp/<name>.yaml`, apply the
     same substitutions, `run_scenario(scenario="/tmp/<name>.yaml")`.
   The stand-side target mapping used in this plan: `6002` → `123001`,
   `6999` → pick a valid stand IVR ext **if one exists** (see B.3 row 5;
   otherwise mark `N/A`).

## Preamble — provision phones

```python
DOMAIN = "192.168.1.202"
PASSWORD = "zzzxxx123"

# Provision 3 phones (a, b, c). None of the 7 example scenarios or
# Pass-A blocks requires more. d..g stay unused — available for ad-hoc
# follow-up scenarios after the main run.
#
# IMPORTANT: `codecs=[...]` in add_phone sets the ENDPOINT codec list
# (see src/server.py:247). So the last call's codecs wins — we use
# ["PCMA", "PCMU"] so A.11's mid-call re-INVITE has somewhere to swap to.
add_phone(phone_id="a", domain=DOMAIN, username="123000", password=PASSWORD,
          codecs=["PCMA", "PCMU"], recording_enabled=True, capture_enabled=True)
add_phone(phone_id="b", domain=DOMAIN, username="123001", password=PASSWORD,
          codecs=["PCMA", "PCMU"], recording_enabled=True)
add_phone(phone_id="c", domain=DOMAIN, username="123002", password=PASSWORD,
          codecs=["PCMA", "PCMU"], recording_enabled=True)

# Sanity — wait up to 5s for all three to publish reg.success.
list_phones()    # expect all three with is_registered=true
```

**STOP** if any phone fails to register. Diagnose via
`get_sip_log(phone_id="a", filter_text="REGISTER")` and verify the stand's
registrar at `192.168.1.202` is reachable from the MCP host (firewall /
VPN). PortaSIP may also enforce NAT/Contact rewriting; if REGISTER fails
with `403`/`401`, try in order:
1. `update_phone(phone_id="a", username="123000@192.168.1.202")` —
   forces SIP URI form in the From/Contact headers.
2. Verify password — copy-paste from the source of truth, no typos.
3. If stand uses TLS/TCP: `update_phone(phone_id="a", transport="tls")`
   (or pass `transport="tcp"` at `add_phone`-time).

---

## Pass A — Classic per-phone tool coverage (sip-phones style)

Pattern used by the existing `sip-phones` skill: you call the per-phone MCP
tools directly (`a_make_call`, `b_answer_call`, …), wait between steps with
polling + `time.sleep`, and inspect state. This pass catches regressions in
the **atomic** tool layer.

For each row: run the tool, verify via the listed assertion. On failure,
capture `get_sip_log(phone_id=..., last_n=30)` into the diagnostic block.

### A.1 Phone lifecycle
| Tool | Expected |
|---|---|
| `list_phones()` | all 3 phones, `is_registered=true` |
| `get_phone("a")` | tools list includes `a_make_call`, `a_answer_call`, … |
| `update_phone("a", auto_answer=True)` | returns updated config; `get_phone` reflects it |
| `update_phone("a", auto_answer=False)` | restored |
| `get_phone_profile_example()` | returns YAML template string |
| `load_phone_profile(path="/config/phones.test.yaml")` | only if a file exists; skip otherwise |

### A.2 Registration
| Tool | Expected |
|---|---|
| `a_get_registration_status()` | `is_registered=True`, `status_code=200` |
| `a_unregister()` | returns ok; status flips to `is_registered=False` |
| `a_register()` | returns ok; status flips back to `is_registered=True` |

### A.3 Simple call + DTMF
```python
r = a_make_call(dest_uri="sip:123001@192.168.1.202")
call_a = r["call_id"]
# poll a_get_call_info until state == CONFIRMED (up to 5s) before DTMF
b_answer_call()
time.sleep(0.5)                      # let media settle
a_send_dtmf(call_id=call_a, digits="1234")
time.sleep(1.0)                      # DTMF RTP events take ~250ms each
a_get_call_info()          # RTP packets_tx/rx > 0, codec "PCMA"
a_hangup()
a_get_call_history(last_n=1)   # duration > 0, correct remote_uri
```
**Pass iff:** both sides reach CONFIRMED, DTMF observed on receiver side
via `get_sip_log(phone_id="b", filter_text="INFO")` (RFC2833 goes as RTP
events, but SIP INFO is emitted by pjsua when configured — if neither is
present, inspect the pcap for RFC2833 DTMF events), clean disconnect.

### A.4 Reject (486)
```python
a_make_call(dest_uri="sip:123001@192.168.1.202")
b_reject_call(status_code=486)
a_get_call_info()    # last_status=486
```

### A.5 Hold / Unhold
```python
r = a_make_call(dest_uri="sip:123001@192.168.1.202")
b_answer_call()
time.sleep(1)
a_hold(call_id=r["call_id"])
time.sleep(2)
a_unhold(call_id=r["call_id"])
time.sleep(2)
a_hangup()
```
**Pass iff:** `get_sip_log(phone_id="a", filter_text="a=sendonly")` has ≥1 entry; `a=sendrecv` has ≥2.

### A.6 Blind transfer
```python
a_make_call("sip:123001@192.168.1.202")
b_answer_call()
time.sleep(1)
b_blind_transfer(dest_uri="sip:123002@192.168.1.202")
time.sleep(3)
# C should now be in a call with A
c_get_active_calls()    # expect 1 entry, remote ≈ A
c_hangup()
```
**Pass iff:** `get_sip_log(filter_text="REFER")` shows B→A REFER.

### A.7 Attended transfer
```python
a_make_call("sip:123001@192.168.1.202");  b_answer_call();  time.sleep(1)
# pick b's current (only) call — the incoming A→B leg
b_calls = b_get_active_calls()
ab_call_on_b = b_calls["calls"][0]["call_id"]
b_hold(call_id=ab_call_on_b)
time.sleep(0.5)
b_make_call("sip:123002@192.168.1.202");  c_answer_call();  time.sleep(1)
# b now has two calls — attended_transfer bridges A↔C with Replaces
b_attended_transfer()
time.sleep(3)
```
**Pass iff:** `get_sip_log(filter_text="Replaces:")` has ≥1 entry **and**
`a_get_active_calls()` shows remote now resolves to c (123002), not b.

### A.8 Conference (local audio bridge)
```python
r1 = a_make_call("sip:123001@192.168.1.202");  b_answer_call();  time.sleep(1)
r2 = a_make_call("sip:123002@192.168.1.202");  c_answer_call();  time.sleep(1)
a_conference(call_ids=[r1["call_id"], r2["call_id"]])
time.sleep(3)
# a_hangup_all() is scenario-engine-only (not registered as per-phone tool)
# — direct-tool users hang up each leg explicitly:
a_hangup(call_id=r1["call_id"])
a_hangup(call_id=r2["call_id"])
```
**Pass iff:** during conference, `a_get_active_calls()` shows 2 calls with
`packets_rx > 0` on both; after the two `a_hangup()` calls the count
drops to 0. The `hangup_all` action itself is covered by Pass B.3 row 4
(conference scenario).

### A.9 Media playback
```python
r = a_make_call("sip:123001@192.168.1.202");  b_answer_call();  time.sleep(1)
a_play_audio(file_path="/app/audio/moh.wav", call_id=r["call_id"])
time.sleep(3)
a_stop_audio(call_id=r["call_id"])
a_hangup()
a_get_recording(call_id=r["call_id"])    # path + size > 0
list_recordings(phone_id="a")            # includes the WAV
```

### A.10 Messaging (out-of-dialog MESSAGE)
```python
a_send_message(dest_uri="sip:123001@192.168.1.202", body="hello")
time.sleep(0.5)
b_get_messages(last_n=1)    # one entry with body="hello"
```

### A.11 Codecs
```python
get_codecs()                                              # endpoint priority list — expect PCMA, PCMU
set_codecs(codecs=["PCMU", "PCMA"])                       # reorder endpoint: PCMU first
# Call with PCMU preferred:
r = a_make_call("sip:123001@192.168.1.202");  b_answer_call();  time.sleep(1)
a_get_call_info()          # expect codec == "PCMU"
# mid-call re-INVITE: downgrade to PCMA-only on this call
set_codecs(codecs=["PCMA"], phone_id="a", call_id=r["call_id"])
time.sleep(2)
a_get_call_info()          # expect codec == "PCMA" after re-INVITE
a_hangup()
# Restore endpoint order for subsequent tests:
set_codecs(codecs=["PCMA", "PCMU"])
```
**Pass iff:** `get_sip_log(phone_id="a", filter_text="a=rtpmap:8")`
shows ≥1 INVITE offering PCMA after the re-INVITE, and `call_info`
codec flips PCMU → PCMA between the two checkpoints. If the stand
rejects PCMU (488 Not Acceptable Here), swap PCMU ↔ G722 and retry — or
mark A.11 as `N/A — stand supports only PCMA`.

### A.12 Introspection tools
| Tool | Expected |
|---|---|
| `get_sip_log(last_n=20)` | returns recent SIP lines |
| `get_sip_log(phone_id="a", filter_text="INVITE")` | only a's INVITEs |
| `a_list_calls()` | all calls on a incl. disconnected |
| `a_get_active_calls()` | only non-DISCONNECTED |
| `a_get_call_history(last_n=3)` | most recent 3 |
| `list_recordings()` | returns recordings across phones |
| `list_recordings(phone_id="a")` | only a's |
| `get_pcap()` | most recent pcap path + size |

### A.13 Capture
```python
# phone b doesn't have capture_enabled — use manual start/stop
start_capture(phone_id="b", interface="any")
r = b_make_call("sip:123000@192.168.1.202");  a_answer_call();  time.sleep(2);  b_hangup()
stop_capture(phone_id="b")
get_pcap()    # path exists
```
**Pass iff:** pcap file > 0 bytes, contains SIP + RTP packets (verify manually if tooling allows).

### A.14 Phone teardown & mutation
Quick smoke of lifecycle tools the rest of Pass A doesn't exercise. Run
this **last** in Pass A so subsequent blocks still have phones to use —
or provision a throwaway `z` phone for it.

```python
# Throwaway phone to avoid disturbing a/b/c:
add_phone(phone_id="z", domain=DOMAIN, username="123006", password=PASSWORD,
          codecs=["PCMA"])
list_phones()                                       # z present, registered
update_phone(phone_id="z", password="wrong-pw")     # should trigger re-register
time.sleep(2)
z_get_registration_status()                         # expect is_registered=false, status in {401,403}
update_phone(phone_id="z", password=PASSWORD)
time.sleep(2)
z_get_registration_status()                         # back to is_registered=true
drop_phone(phone_id="z")                            # tools should disappear
list_phones()                                       # no z entry
```
**Pass iff:** status flips on bad password and recovers on good password;
`drop_phone` removes z from `list_phones` and unregisters its MCP tools.

---

## Pass B — Scenario engine coverage

This pass verifies the **new** layer: scenario YAML → engine → same
pjsua tool calls under the hood. Compare timeline output against Pass A
observations.

### B.1 Discovery

```python
list_patterns()                 # expect 14 entries
list_patterns(tags=["dtmf"])    # subset — send-dtmf-on-confirmed,
                                # respond-to-dtmf
list_patterns(query="transfer") # subset — blind-transfer
```
**Pass iff:** count == 14 for unfiltered; filters return subsets.

```python
get_pattern(name="auto-answer")
```
**Pass iff:** response has `body_template` (raw Jinja) AND `rendered_body`
(resolved hooks/initial_actions/expected_timeline) when the pattern ships
examples.

```python
get_scenario_template()
```
**Pass iff:** returns `template` (YAML skeleton), `events` (dict of event
categories), `actions` (dict of action categories), `docs` (list of paths).

### B.2 Static validation

```python
# Valid scenario
validate_scenario(scenario={
    "patterns": [{"use": "auto-answer", "phone_id": "a"}],
    "stop_on": [{"phone_id": "a", "event": "user.done"}],
})
# expect status="ok", issues=[]

# Unknown pattern
validate_scenario(scenario={"patterns": [{"use": "does-not-exist", "phone_id": "a"}]})
# expect status="error", one issue with kind="pattern_ref"

# Unknown action
validate_scenario(scenario={
    "initial_actions": [{"frobnicate": 42}],
})
# expect status="error", one issue with kind="initial_action"

# Unknown event type in stop_on
validate_scenario(scenario={"stop_on": [{"event": "totally.fake"}]})
# expect status="error", one issue with kind="stop_on"
```
**Pass iff:** all four return expected status + shape of issues.

### B.3 Example scenarios end-to-end

Run each of `scenarios/examples/*.yaml` via `run_scenario`, applying the
URI substitution from Precondition §5 (hard-coded `@asterisk` targets
won't resolve on PortaSIP). Recommended flow: read YAML → parse to dict
→ substitute `sip:<ext>@asterisk` → stand ext + `@192.168.1.202` → call
`run_scenario(scenario=<dict>)`. Per-row target mapping:

| # | Example file | Asterisk target | PortaSIP substitute |
|---|---|---|---|
| 1 | hello-world.yaml | `sip:6002@asterisk` | `sip:123001@192.168.1.202` |
| 2 | blind-transfer.yaml | `sip:6002@asterisk`, `sip:6003@asterisk` | `sip:123001@...`, `sip:123002@...` |
| 3 | attended-transfer.yaml | `sip:6002@asterisk`, `sip:6003@asterisk` | `sip:123001@...`, `sip:123002@...` |
| 4 | conference.yaml | `sip:6002@asterisk`, `sip:6003@asterisk` | `sip:123001@...`, `sip:123002@...` |
| 5 | ivr-navigation.yaml | `sip:6999@asterisk` (AA) | **ask user** for stand IVR ext; else `N/A` |
| 6 | sequence-calls.yaml | multiple `@asterisk` | substitute all to `123001..123003` |
| 7 | sip-14744-minimal.yaml | see YAML | substitute per file |

Read the YAML before the run to enumerate every `@asterisk` string you
need to substitute — don't trust this table blindly if a file was edited.

| # | Scenario | Expected status | Key timeline evidence |
|---|---|---|---|
| 1 | hello-world | ok | `action.send_dtmf` with digits="1", clean `call.state.disconnected` on a |
| 2 | blind-transfer | ok | `action.blind_transfer`; sip_log has REFER |
| 3 | attended-transfer | ok | sip_log has `Replaces:` |
| 4 | conference | ok | `action.conference`; 2× active calls on a before `action.hangup_all` |
| 5 | ivr-navigation | ok *or N/A* | three `action.send_dtmf` entries ("1", "2", "*") + `action.hangup`. ⚠️ Skeleton YAML targets `sip:6999@asterisk` (AA) — if PortaSIP stand has no AA/IVR extension, either (a) substitute a stand-side IVR ext if the user provides one, or (b) skip this row and mark `N/A — no IVR ext on stand` in the report. |
| 6 | sequence-calls | ok | 2× outbound make_call, 2× clean disconnect |
| 7 | sip-14744-minimal | ok (preferred) or documented timeout | Pass criteria: both phones reach `call.state.confirmed` **and** ≥1 `dtmf.out` in timeline. Timeout allowed only if scenario's own `timeout_ms` is hit — capture the last 20 timeline entries for the report. |

For each scenario, AFTER the run also verify (adjusting `phone_id` to
whichever phone actually initiated calls — usually `a`, but check the
YAML first; only `a` has `capture_enabled=True` from the preamble):
- `list_recordings(phone_id="<originator>")` added fresh WAVs (all 3
  phones have `recording_enabled=True`, so any leg generates a WAV)
- `get_pcap()` returns a fresh pcap path (only `a`'s traffic is in it)
- `get_sip_log(phone_id="<originator>", last_n=40)` has the expected
  INVITE/200/BYE flow

### B.4 run_scenario via inline dict (no file)

Author one scenario inline via `run_scenario(scenario={...dict...})` to
verify dict-mode works identically to file-mode. Use the hello-world
shape from `docs/agent-handoff.md`.

---

## Pass C — Event & Action coverage matrix

Ensures every emit point in the engine fires at least once, and every
action reaches the CallManager / PhoneRegistry / SipEngine under the hood.

### C.1 Event coverage

For each event type, identify which scenario in Pass B **must** have caused
it to appear in the timeline. Scan `result["timeline"]` and mark each row.

| Event | Expected in |
|---|---|
| `reg.started` | fires during `add_phone` preamble — observe via `get_sip_log(phone_id="a", filter_text="REGISTER")` (≥1 REGISTER before 200 OK) |
| `reg.success` | after preamble — `list_phones()` shows `is_registered=true` for all |
| `reg.failed` | **no scenario action exists** — drive via MCP tools directly: `update_phone(phone_id="a", password="wrong"); a_register()`. Verify via `a_get_registration_status()` → `status_code` in `{401, 403}` and `is_registered=false`. Restore: `update_phone(phone_id="a", password="zzzxxx123"); a_register()` |
| `reg.unregistered` | **no scenario action exists** — drive via MCP tools: `a_unregister()`. Verify via `a_get_registration_status()` → `is_registered=false` and `get_sip_log(phone_id="a", filter_text="REGISTER")` shows a final REGISTER with `Expires: 0`. Restore: `a_register()` |

**Note:** `reg.failed` and `reg.unregistered` cannot be exercised through
`run_scenario` because the engine has no `register`/`unregister` actions
(see `src/scenario_engine/action_executor.py` dispatch table). Treat
these two rows as direct-tool coverage, not scenario coverage. If future
work adds those actions, collapse this into a simple 3-line scenario.
| `call.state.calling` | hello-world (A side after `make_call`) |
| `call.state.incoming` | hello-world (B side) |
| `call.state.early` | any scenario with a ~180 Ringing pause (A before B picks up) |
| `call.state.connecting` | any scenario (transient) |
| `call.state.confirmed` | hello-world |
| `call.state.disconnected` | every scenario |
| `dtmf.in` | hello-world B-side (when A sends DTMF) |
| `dtmf.out` | hello-world A-side |
| `im.received` | instant-message smoke (Pass A.10 or author a 2-line scenario with `send_message`) |
| `scenario.started`, `scenario.stopped` | every scenario |
| `user.<name>` | ivr-navigation (if it uses `emit`) or conference (author an `emit` checkpoint) |

If an event is not observed in any scenario, author a **minimal trigger
scenario** (3-5 lines YAML) just to fire it, and note which row was covered
by that synthetic scenario.

### C.2 Action coverage

For each of the 19 actions, identify which scenario dispatched it. Cross-
reference `timeline[*].kind == "action"` entries.

| Action | Covered by |
|---|---|
| `answer` | hello-world (via auto-answer pattern on b) |
| `hangup` | hello-world |
| `hangup_all` | conference |
| `reject` | Pass A.4 or author a scenario with `reject-on-incoming` pattern |
| `hold` / `unhold` | attended-transfer (hold); author for unhold if needed |
| `send_dtmf` | hello-world, ivr-navigation |
| `blind_transfer` | blind-transfer |
| `attended_transfer` | attended-transfer |
| `conference` | conference |
| `make_call` | hello-world (initial action from pattern) |
| `play_audio` / `stop_audio` | author a 3-line scenario using `play-audio-on-confirmed` pattern |
| `send_message` | instant-message smoke |
| `set_codecs` | codec-change smoke (use `reinvite-codec-change` pattern) |
| `wait`, `wait_until`, `emit`, `checkpoint`, `log` | exercised implicitly by many scenarios; look for `kind="meta"` timeline entries |

Any action with **no hit** across all scenarios is a coverage gap. Author
a minimal scenario and record it.

---

## Report format

Produce one markdown report. Save it (if file-write is available) to
`docs/integration-runs/YYYY-MM-DD-HHMM.md`, otherwise emit inline in chat.

```markdown
# Integration run 2026-04-24 21:00

## Environment
- SIP stand: PortaSIP at 192.168.1.202 (reachability check: `ping` + `sip-options` before run)
- pjsua MCP: <branch + sha>
- Phone profile: 123000..123002 (a/b/c), 123003..123006 available for multi-phone scenarios

## Pass A — per-phone tools (14 rows)
| # | Area | Status | Notes |
|---|---|---|---|
| A.1 | Phone lifecycle | ✅ | — |
| A.2 | Registration | ✅ | — |
| A.3 | Simple call + DTMF | ✅ | 4 DTMF delivered in ~2.1s |
| A.4 | Reject 486 | ✅ | — |
| A.5 | Hold/Unhold | ⚠️ flaky | 2nd re-INVITE delayed once of 3 runs |
| ... | ... | ... | ... |

## Pass B — scenario engine (B.1–B.4)
| # | Check | Status | Notes |
|---|---|---|---|
| B.1 | Discovery | ✅ | 14 patterns, get_pattern returns rendered_body |
| B.2 | Validation | ✅ | all 4 shapes rejected correctly |
| B.3.1 | hello-world | ✅ | elapsed=5.8s, timeline clean |
| B.3.2 | blind-transfer | ❌ | REFER seen but B's sip_log shows NOTIFY failure — see diag 1 |
| ...  | ... | ... | ... |

## Pass C — coverage matrix
- Events: 15/16 covered (reg.failed not exercised — author forgot)
- Actions: 19/20 covered (set_codecs missing — no scenario run)

## Diagnostics
### diag 1 — blind-transfer NOTIFY failure
<last 10 timeline entries + relevant get_sip_log>

## Open issues for follow-up
1. <…>
```

---

## What to do on failure

- **Pass A row fails:** regression in atomic tool. File the bug with the
  reproducer (exact tool sequence) and the captured sip_log / timeline.
- **Pass B scenario fails but Pass A underlying flow works:** engine wiring
  issue. Likely candidates: hook match predicate, stop_on filter,
  `call_id` resolution in ActionExecutor. Attach timeline + pattern
  rendered_body + scenario YAML.
- **Pass C gap (event/action not observed):** not a bug, just coverage
  gap. Author a minimal scenario to close.

## What NOT to do

- Don't run production-impacting tools against a real PBX. This plan
  targets the dedicated PortaSIP test stand at `192.168.1.202` — confirm
  with the user if you're unsure it's the test instance and not
  production.
- Don't modify patterns or examples mid-run — if you spot a bug, note it
  and continue; the fix is a separate PR.
- Don't push to remote / raise PRs without explicit user ask.
