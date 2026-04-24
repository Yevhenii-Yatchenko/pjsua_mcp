# pjsua Scenarios — Pattern-Based SIP Test Runner

Event-driven scenario engine for the pjsua MCP server. Instead of orchestrating
multi-step SIP calls via many separate tool calls (with 1–3 s LLM-turn latency
each), scenarios are described once as a composition of **patterns** and run
with a single `run_scenario` call. The engine executes hooks and actions inside
its own asyncio loop — SIP timings become deterministic.

## Quick start

```python
# 1. Provision phones (ordinary MCP tools — not part of scenario engine)
add_phone(phone_id="a", domain="asterisk", username="6001", password="test123")
add_phone(phone_id="b", domain="asterisk", username="6002", password="test123",
          auto_answer=False)

# 2. List available patterns
list_patterns()                   # all 20
list_patterns(tags=["dtmf"])      # narrow by tag
get_pattern("auto-answer")        # full spec

# 3. Compose + run
run_scenario(scenario={
    "name": "a-calls-b-with-dtmf",
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

Result:
```python
{
    "status": "ok",
    "elapsed_ms": 5843.2,
    "timeline": [
        {"kind": "meta", "ts_offset_ms": 0, "type": "scenario.started", ...},
        {"kind": "event", "ts_offset_ms": 152, "type": "reg.success", "phone_id": "a", ...},
        {"kind": "event", "ts_offset_ms": 201, "type": "reg.success", "phone_id": "b", ...},
        {"kind": "action", "ts_offset_ms": 251, "type": "make_call", "phone_id": "a", ...},
        {"kind": "event", "ts_offset_ms": 340, "type": "call.state.incoming", "phone_id": "b", ...},
        {"kind": "action", "ts_offset_ms": 840, "type": "answer", "phone_id": "b", ...},
        ...
    ],
    "errors": [],
    "patterns_used": ["wait-for-registration@1.0.0", ...]
}
```

## Pattern catalog (14 atomic patterns)

Patterns are deliberately kept **atomic** — one hook or one initial_action
wrapping one MCP action with an optional delay. Composite flows (attended
transfer, conferences, IVR navigation, etc.) live as **example scenarios**
that use **inline `hooks:`** — see `scenarios/examples/`.

| Name | Listens to | Dispatches |
|---|---|---|
| `wait-for-registration` | `reg.success`, `reg.failed` | — |
| `auto-answer` | `call.state.incoming` | `answer` (after optional delay) |
| `make-call-and-wait-confirmed` | — (initial action) | `make_call` |
| `send-dtmf-on-confirmed` | `call.state.confirmed` | `send_dtmf` |
| `hangup-after-duration` | `call.state.confirmed` | `wait` + `hangup` |
| `reject-on-incoming` | `call.state.incoming` | `reject` |
| `blind-transfer` | `call.state.confirmed` | `wait` + `blind_transfer` |
| `hold-and-resume` | `call.state.confirmed` | `wait`+`hold`+`wait`+`unhold` |
| `wait-for-callback` | `call.state.incoming` | `answer` (optional) |
| `respond-to-dtmf` | `dtmf.in` (matched digit) | user-chosen action |
| `fail-fast-on-error` | `call.state.disconnected` (bad status) | `emit scenario_failed` |
| `play-audio-on-confirmed` | `call.state.confirmed` | `play_audio` |
| `reinvite-codec-change` | `call.state.confirmed` | `wait` + `set_codecs` |
| `collect-recordings` | `scenario.stopped` | `log` per phone |

## Event taxonomy (MVP)

- **Call state:** `call.state.{calling,incoming,early,connecting,confirmed,disconnected}`
- **DTMF:** `dtmf.in`, `dtmf.out`
- **Registration:** `reg.{started,success,failed,unregistered}`
- **Scenario:** `scenario.{started,stopped}`
- **User-emitted:** `user.<name>` (from `emit` action)

## Action vocabulary

| Action | Args | Maps to |
|---|---|---|
| `answer` | `code` (default 200) | `Call.answer()` |
| `hangup` | — | `Call.hangup()` |
| `reject` | `code` (default 486) | `Call.hangup(code)` |
| `hold` / `unhold` | — | `Call.setHold()` / `reinvite()` |
| `send_dtmf` | `digits` or value | `Call.dialDtmf()` |
| `blind_transfer` | `to` (sip-uri) | `Call.xfer()` (REFER) |
| `make_call` | `phone_id`, `to`, `headers?` | `CallManager.make_call()` |
| `wait` | `ms` or value (`"500ms"`, `"2s"`) | `asyncio.sleep` |
| `wait_until` | `event`, `timeout_ms?` | `EventBus.wait_for` |
| `emit` | `name`, `data?` | Inject user event |
| `checkpoint` | `label` | Timeline marker |
| `log` | `message` | Timeline log entry |

## Inline hooks — when you don't need a pattern

Scenarios can declare `hooks:` directly — one-off logic doesn't need a
pattern wrapper. This is the shape most `scenarios/examples/` use for
composite flows (attended transfer, conferences, IVR navigation):

```yaml
name: my-scenario
phones: [a, b]
patterns:
  - {use: auto-answer, phone_id: b}
  - {use: make-call-and-wait-confirmed, phone_id: a, dest_uri: "sip:..."}
hooks:
  - when: call.state.confirmed
    on_phone: a
    once: true
    then:
      - wait: 1s
      - hold
      - make_call: {phone_id: a, to: "sip:other@asterisk"}
      - wait: 2s
      - attended_transfer
stop_on:
  - {phone_id: a, event: call.state.disconnected, call_id: 1}   # call_id filter
  - {event: "call.state.disconnected", match: {last_status: "4xx"}}  # match predicate
```

**Rule of thumb:** if a flow appears in 3+ scenarios, extract it as a
pattern. Until then, keep it inline.

## `stop_on` filtering

A `stop_on` entry can filter events by:
- `phone_id` — only events on this phone
- `call_id` — only events on this specific call (useful when multiple calls
  exist on one phone — attended transfer, conference)
- `match: {field: value}` — arbitrary field predicate on `event.data`
  (supports `"4xx"`/`"5xx"` status classes, `~regex` strings, list membership)

## When NOT to use scenarios

- **Exploratory debugging** — manual `<phone>_make_call` / `<phone>_send_dtmf`
  tools are faster for one-off prodding.
- **Interactive flows** where a human's input changes mid-call — scenarios are
  declarative, designed for deterministic reproduction.
- **SIP-header-level matching** — not yet wired in MVP. Workaround: call
  `get_sip_log(filter_text=...)` after the scenario returns.

## Writing a new pattern

1. Copy an existing YAML in `scenarios/patterns/` as a template.
2. Edit `name`, `version`, `description`, `params`.
3. Write `hooks` and `expected_timeline`.
4. Add at least one `example:` invocation.
5. Run `pytest tests/scenario_engine/test_pattern_loader.py` — the new file
   is auto-discovered by parametrized schema tests.
6. If the pattern uses an action not yet in the executor
   (`src/scenario_engine/action_executor.py`), wire it there too.

See `scenarios/patterns/SCHEMA.md` for the formal format specification.

## Debugging a scenario that didn't behave

Read the `timeline` in the `run_scenario` result:

- **Hook never fired** → its `when:` event was not emitted, or the
  `on_phone` / `match` predicate filtered it out. Look at what events
  *did* fire in the timeline.
- **Wrong timing** → check `ts_offset_ms` for the action; compare against
  the pattern's `expected_timeline`.
- **Scenario timed out** → no `stop_on` event matched; either fix the
  condition or shorten `timeout_ms`.
- **Pattern failed to load** → check the `errors` list in the result.

## File layout

- `scenarios/patterns/*.yaml` — the 20 pattern templates
- `scenarios/examples/*.yaml` — demo scenarios
- `scenarios/patterns/SCHEMA.md` — formal pattern spec
- `src/scenario_engine/` — the runtime (event bus, pattern loader, hook
  runtime, action executor, orchestrator, timeline)
- `tests/scenario_engine/` — unit + symbolic tests (94 tests, <2 s)
- `.claude/skills/pjsua-scenarios/SKILL.md` — Claude Code skill (local only)
