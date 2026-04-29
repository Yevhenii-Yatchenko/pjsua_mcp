"""Unit tests for action shorthand grammar — normalize_action and _parse_ms.

Both are the front line for diagnosing 'why doesn't my YAML work'. Cover
every documented form (reference.md §6) plus the malformed-input paths.
"""

from __future__ import annotations

import pytest

from src.scenario_engine.action_executor import (
    ActionError,
    _parse_ms,
    normalize_action,
)


# ----------------------------- normalize_action -----------------------------

@pytest.mark.parametrize("spec, expected", [
    # Bare string
    ("answer", ("answer", {})),
    ("hangup", ("hangup", {})),

    # Dict, empty args
    ({"answer": {}}, ("answer", {})),

    # Dict with arg dict
    ({"answer": {"code": 200}}, ("answer", {"code": 200})),
    ({"reject": {"code": 486}}, ("reject", {"code": 486})),

    # Dict with scalar value (becomes `value` key)
    ({"send_dtmf": "1234"}, ("send_dtmf", {"value": "1234"})),
    ({"wait": "500ms"}, ("wait", {"value": "500ms"})),
    ({"wait": 1500}, ("wait", {"value": 1500})),
    ({"checkpoint": "stage-1"}, ("checkpoint", {"value": "stage-1"})),
    ({"emit": "my_signal"}, ("emit", {"value": "my_signal"})),
    ({"reject": 603}, ("reject", {"value": 603})),
    ({"blind_transfer": "sip:6003@asterisk"},
     ("blind_transfer", {"value": "sip:6003@asterisk"})),

    # Dict with explicit `action:` key
    ({"action": "send_dtmf", "digits": "1"},
     ("send_dtmf", {"digits": "1"})),
    ({"action": "make_call", "phone_id": "a", "dest_uri": "sip:x"},
     ("make_call", {"phone_id": "a", "dest_uri": "sip:x"})),

    # Dict with `action:` plus extra siblings — explicit form survives extra args
    ({"action": "set_codecs", "codecs": ["G722"], "phone_id": None},
     ("set_codecs", {"codecs": ["G722"], "phone_id": None})),

    # Dict with single key whose value is None (no args)
    ({"hangup": None}, ("hangup", {})),
])
def test_normalize_action_accepts(spec, expected) -> None:
    assert normalize_action(spec) == expected


@pytest.mark.parametrize("bad_spec", [
    # Multi-key dict with no `action:` — ambiguous
    {"a": 1, "b": 2},
    {"answer": {}, "hangup": {}},

    # Unsupported root types
    42,
    [1, 2, 3],
    None,
])
def test_normalize_action_rejects(bad_spec) -> None:
    with pytest.raises(ActionError):
        normalize_action(bad_spec)


# ----------------------------- _parse_ms -----------------------------

@pytest.mark.parametrize("value, expected_ms", [
    # Integer ms
    (0, 0),
    (1, 1),
    (1500, 1500),

    # Float ms — truncated to int
    (500.5, 500),
    (1.9, 1),

    # String ms
    ("500ms", 500),
    ("0ms", 0),
    ("100 ms", 100),    # whitespace tolerated

    # String s — converted to ms
    ("2s", 2000),
    ("1.5s", 1500),
    ("0.25s", 250),
    ("0s", 0),

    # Bare numeric strings — interpreted as ms
    ("250", 250),
    ("0", 0),
])
def test_parse_ms_accepts(value, expected_ms) -> None:
    assert _parse_ms(value) == expected_ms


@pytest.mark.parametrize("bad_value", [
    "abc",          # non-numeric
    "500foo",       # bogus suffix
    "",             # empty
    "ms",           # no number
    None,           # type unsupported
    [500],          # list
    {"ms": 500},    # dict
])
def test_parse_ms_rejects(bad_value) -> None:
    with pytest.raises(ActionError):
        _parse_ms(bad_value)
