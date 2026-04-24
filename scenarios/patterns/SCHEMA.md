# Pattern Format Specification

A pattern is a **reusable, parameterised scenario fragment** for the pjsua MCP
scenario engine. One `.yaml` file per pattern. Patterns are composed into
scenarios via `{use: <pattern-name>, <params>}` references.

## File Layout

Every pattern file is **two YAML documents separated by a `---` line**:

```yaml
# Document 1 — metadata (pure YAML, no Jinja)
name: auto-answer
version: 1.0.0
description: Answer every incoming call on the target phone.
tags: [incoming, basic]
params:
  phone_id: {type: string, required: true}
  delay_ms: {type: integer, default: 0}
examples:
  - use: auto-answer
    phone_id: b
---
# Document 2 — body template (Jinja2 → YAML)
hooks:
  - when: call.state.incoming
    on_phone: "{{ phone_id }}"
    then:
      {% if delay_ms > 0 %}
      - wait: "{{ delay_ms }}ms"
      {% endif %}
      - answer
expected_timeline:
  - event: call.state.incoming
  - action: answer
    after_ms: "{{ delay_ms }}"
```

**Why the split?** Document 1 lets the scenario runner read metadata (name,
params schema, examples) without first rendering the template. Document 2 is
only parsed after Jinja has substituted the resolved parameter values.

## Metadata fields (Document 1)

| Field | Required | Purpose |
|---|---|---|
| `name` | yes | snake-case, max 64 chars, unique across the library |
| `version` | yes | semver (`major.minor.patch`) |
| `description` | yes | one-liner explaining what the pattern does |
| `tags` | recommended | taxonomy for discovery — see **Tag vocabulary** below |
| `rationale` | optional | multi-paragraph why-this-exists |
| `params` | recommended | per-param JSONSchema with `required` / `default` |
| `examples` | recommended | invocations that exercise typical use |
| `requires` | optional | other patterns this depends on (e.g. `wait-for-registration@^1`) |
| `failure_modes` | recommended | known-bad paths (helps LLM diagnose failures) |
| `compatibility` | optional | `{mcp_server: ">=0.5.0", requires_tools: [...]}` |

## Body fields (Document 2)

| Field | Purpose |
|---|---|
| `hooks` | list of event-triggered reactions |
| `initial_actions` | actions fired once at scenario start, independent of events |
| `expected_timeline` | optional; used by symbolic tests to validate pattern wiring |

### Hook structure

```yaml
- when: <event-type> | [<event-type>, ...]    # required
  on_phone: <phone_id>                        # optional filter
  match: {field: value}                       # optional predicate on event.data
  then: [<action>, ...]                       # required (can be [])
  once: bool                                  # default true
```

Wildcards: `"call.state.*"` matches all call.state.* events. `"*"` matches all.

### Action vocabulary

Actions fall into three groups. Each action maps to an MCP-level tool or
pjsua manager method — the column **Maps to** is authoritative.

#### Call-control actions

| Action | Args | Maps to |
|---|---|---|
| `answer` | `code: int` (default 200) | `CallManager.answer_call` |
| `hangup` | — | `CallManager.hangup` (single call) |
| `hangup_all` | `phone_id?: str` | `CallManager.hangup_all` (all calls on phone, or global if omitted) |
| `reject` | `code: int` (default 486) | `CallManager.reject_call` |
| `hold` / `unhold` | — | `CallManager.hold` / `unhold` (re-INVITE sendonly / sendrecv) |
| `send_dtmf` | `digits: str` (or value) | `CallManager.send_dtmf` (RFC2833 or SIP-INFO) |
| `blind_transfer` | `to: sip-uri` | `CallManager.blind_transfer` (REFER, no Replaces) |
| `attended_transfer` | `call_id`, `dest_call_id` | `CallManager.attended_transfer` (REFER/Replaces) |
| `conference` | `call_ids: [int]` | `CallManager.conference` (pjsua audio-bridge, NOT SIP conf) |
| `make_call` | `phone_id`, `to: sip-uri`, `headers?: dict` | `CallManager.make_call` |

#### Media & messaging

| Action | Args | Maps to |
|---|---|---|
| `play_audio` | `file: str`, `loop?: bool` | `CallManager.play_audio` (WAV → RTP) |
| `stop_audio` | — | `CallManager.stop_audio` (restores default MOH) |
| `send_message` | `to: sip-uri`, `body: str`, `content_type?: str` | `PhoneRegistry.send_message` (out-of-dialog MESSAGE) |
| `set_codecs` | `codecs: [str]` (e.g. `["PCMA"]`) | `SipEngine.set_codecs` — endpoint-wide; if `call_id` provided, triggers re-INVITE |

#### Flow control & meta

| Action | Args | Notes |
|---|---|---|
| `wait` | `ms: int` (or value; `"500ms"`, `"2s"`) | asyncio.sleep within hook execution |
| `wait_until` | `event: type`, `timeout_ms?: int` | block until event matching type arrives |
| `emit` | `name: str`, `data?: dict` | push a `user.<name>` event into the bus |
| `checkpoint` | `label: str` | timeline marker |
| `log` | `message: str` | free-form timeline entry |

### MCP tools NOT yet exposed as scenario actions

The following pjsua MCP tools exist but are **not wired** into the scenario
action executor. Reasons vary (lifecycle that belongs outside scenarios,
read-only diagnostics, or simply not yet prioritised):

| MCP tool | Why not an action (yet) |
|---|---|
| `add_phone` / `drop_phone` | Phone provisioning is scenario-setup, done before `run_scenario` |
| `update_phone` | Could be action (e.g. mid-call toggle `recording_enabled`); not yet wired |
| `register` / `unregister` | Follow-up — needed for REGISTER-refresh / roaming tests |
| `start_capture` / `stop_capture` | Follow-up — most phones use `capture_enabled` profile setting |
| `load_phone_profile` / `get_phone_profile_example` | Pure setup, not scenario-level |
| `get_call_info`, `list_calls`, `get_active_calls`, `get_call_history` | Read-only diagnostics — future `assert` / `snapshot` action |
| `get_sip_log`, `get_pcap`, `list_recordings`, `get_recording` | Read-only artefacts — future `snapshot` action |
| `get_codecs`, `get_registration_status`, `get_messages`, `get_phone`, `list_phones` | Read-only — future `snapshot` action |

If you need one of these in a pattern *today*, the options are:
1. Call the corresponding MCP tool **outside** of the scenario (before
   `run_scenario` for setup, after for introspection).
2. Wire the action in `src/scenario_engine/action_executor.py` and add an
   entry to the table above.

### Default resolution for actions

Actions inherit these defaults from the triggering context:
- `phone_id`: hook's `on_phone`, or the event's `phone_id`
- `call_id`: the triggering event's `call_id`

Any action can override by specifying the field explicitly.

## Event taxonomy (MVP subset)

- **Call state**: `call.state.{calling,incoming,early,connecting,confirmed,disconnected}`
- **DTMF**: `dtmf.in`, `dtmf.out`
- **Registration**: `reg.{started,success,failed,unregistered}`
- **Timer / synthetic**: `scenario.{started,stopped}`, `user.<name>` (from `emit`)

Each event has: `type`, `timestamp`, `phone_id?`, `call_id?`, `data` (dict).

## Tag vocabulary

Use at least two tags per pattern — one role, one capability.

**Role**: `setup`, `teardown`, `caller`, `callee`, `coordinator`
**Capability**: `dtmf`, `transfer`, `hold`, `conference`, `ivr`, `callback`, `registration`, `incoming`, `outgoing`, `basic`, `timer`
**Concurrency**: `single-phone`, `multi-phone`, `race-prone`

## Versioning policy

- Patterns follow semver.
- Scenarios can pin a version: `{use: auto-answer@^1.0.0, ...}`.
- MVP: version constraint is warn-only (log, continue); hard-fail in a future
  release.

## Jinja context

Available in Document 2 templates:

- `{{ <param_name> }}` — resolved param value
- Custom filters: `| sip_user`, `| sip_host`, `| ms_to_sec`, `| as_int`, `| as_str`
- Conditional blocks via `{% if %}...{% endif %}`, loops via `{% for %}`

Keep Jinja blocks short — if a pattern exceeds ~10 lines of `{% ... %}`, split
it into two patterns. The point is a readable template, not a mini-program.

## Scenarios — inline `hooks:` and filtered `stop_on`

A scenario is a plain YAML file (no doc split, no Jinja) that composes
patterns into a runnable unit. In addition to `patterns:` and
`initial_actions:`, scenarios support:

```yaml
name: my-scenario
phones: [a, b, c]
patterns: [...]            # library fragments
initial_actions: [...]     # fired once at scenario start
hooks:                     # same shape as pattern hooks — live only for this run
  - when: call.state.confirmed
    on_phone: a
    once: true
    then: [wait 1s, hold, make_call {...}, wait 2s, attended_transfer]
stop_on:
  - phone_id: a
    event: call.state.disconnected
    call_id: 2             # filter by specific call_id
  - event: call.state.disconnected
    match:
      last_status: "4xx"   # match predicate on event.data fields
timeout_ms: 20000
```

**When to use inline hooks.** One-off composite flows. If the same hook
shape repeats in 3+ scenarios, extract it into a pattern under
`scenarios/patterns/`.

**`stop_on` filter fields.** All optional; combine as needed:

- `event` — the event type (required)
- `phone_id` — match this phone only
- `call_id` — match this specific call id (useful when multiple calls exist)
- `match: {field: value}` — arbitrary predicate over `event.data`; supports
  exact equality, lists (`[200, 486]`), status classes (`"4xx"`, `"5xx"`),
  and regex strings (`"~Q\\.850"`).

## Pre-flight validation

`run_scenario` auto-calls `validate_scenario` as its first step. Any of
these issues return `status="error"` immediately (no wall-clock wasted):

- unknown pattern name / version pin
- pattern param missing or failing schema
- hook `when:` / `stop_on[*].event:` with unknown event prefix
- `then:` / `initial_actions:` containing an action not in the executor's
  dispatch table
- malformed hook (missing `when:`)

To bypass (engine-internal testing only): call `run_scenario(...,
skip_validation=True)` or the standalone `validate_scenario(scenario)` MCP
tool for a detailed report without running.
