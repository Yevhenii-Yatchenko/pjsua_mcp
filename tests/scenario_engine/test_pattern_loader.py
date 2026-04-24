"""Level-1 schema tests: every pattern YAML in scenarios/patterns/ must

- parse as YAML
- satisfy pattern metadata JSONSchema
- render with its first `examples:` entry as params
- have the rendered body satisfy the body JSONSchema

Parametrized over all discovered patterns so new additions are auto-tested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.scenario_engine.pattern_loader import (
    Pattern,
    PatternError,
    PatternRegistry,
    instantiate_pattern,
    load_pattern_template,
)

PATTERNS_DIR = Path(__file__).resolve().parents[2] / "scenarios" / "patterns"

# Collect patterns once, at module import, so parametrize ids are stable.
_all_patterns = sorted(PATTERNS_DIR.glob("*.yaml"))
_pattern_ids = [p.stem for p in _all_patterns]


def test_patterns_directory_exists() -> None:
    assert PATTERNS_DIR.is_dir(), f"expected patterns directory at {PATTERNS_DIR}"


def test_at_least_five_patterns_present() -> None:
    assert len(_all_patterns) >= 5, f"need ≥5 patterns; found {len(_all_patterns)}"


@pytest.mark.parametrize("path", _all_patterns, ids=_pattern_ids)
def test_metadata_schema(path: Path) -> None:
    """Metadata (doc 1) parses and satisfies META_SCHEMA."""
    tmpl = load_pattern_template(path)
    assert tmpl.name == path.stem, f"file {path.name} declares name={tmpl.name!r}"
    assert tmpl.version, "version is required"
    assert tmpl.meta.get("description"), "description is required"


@pytest.mark.parametrize("path", _all_patterns, ids=_pattern_ids)
def test_has_examples(path: Path) -> None:
    """Every pattern must declare at least one example invocation."""
    tmpl = load_pattern_template(path)
    examples = tmpl.meta.get("examples", [])
    assert examples, f"{path.name}: no `examples:` block — can't symbolically test"


@pytest.mark.parametrize("path", _all_patterns, ids=_pattern_ids)
def test_instantiates_with_example_params(path: Path) -> None:
    """Pattern renders + parses when invoked with its first example."""
    tmpl = load_pattern_template(path)
    example = tmpl.meta["examples"][0]
    params = {k: v for k, v in example.items() if k != "use"}
    pat = instantiate_pattern(tmpl, params)
    assert isinstance(pat, Pattern)
    # Hooks is always a list (possibly empty)
    assert isinstance(pat.hooks, list)
    assert isinstance(pat.initial_actions, list)
    assert isinstance(pat.expected_timeline, list)


def test_registry_discovers_all_patterns() -> None:
    """PatternRegistry scans the directory and loads every yaml file."""
    reg = PatternRegistry(PATTERNS_DIR)
    reg.scan()
    assert reg.errors() == {}, f"pattern load errors: {reg.errors()}"
    assert set(reg.names()) == set(_pattern_ids)


def test_registry_filter_by_tags() -> None:
    reg = PatternRegistry(PATTERNS_DIR)
    reg.scan()
    dtmf_patterns = reg.list(tags=["dtmf"])
    names = {p["name"] for p in dtmf_patterns}
    # send-dtmf-on-confirmed + respond-to-dtmf + ivr-navigation all have dtmf tag
    assert "send-dtmf-on-confirmed" in names
    assert "respond-to-dtmf" in names


def test_registry_filter_by_query() -> None:
    reg = PatternRegistry(PATTERNS_DIR)
    reg.scan()
    hits = reg.list(query="transfer")
    names = {p["name"] for p in hits}
    assert "blind-transfer" in names


def test_missing_required_param_raises() -> None:
    reg = PatternRegistry(PATTERNS_DIR)
    reg.scan()
    # auto-answer requires phone_id
    with pytest.raises(PatternError, match="phone_id"):
        reg.instantiate("auto-answer", {})


def test_unknown_pattern_raises() -> None:
    reg = PatternRegistry(PATTERNS_DIR)
    reg.scan()
    with pytest.raises(PatternError, match="not found"):
        reg.instantiate("does-not-exist", {})


def test_param_schema_validation_rejects_bad_type() -> None:
    reg = PatternRegistry(PATTERNS_DIR)
    reg.scan()
    # phone_id expects string matching ^[a-z0-9_]{1,32}$
    with pytest.raises(PatternError):
        reg.instantiate("auto-answer", {"phone_id": "INVALID-UPPERCASE"})
