"""Pattern loader — reads YAML templates, renders with Jinja, validates structure.

Pattern file layout (two YAML documents separated by `---`):

    name: auto-answer
    version: 1.0.0
    description: ...
    tags: [incoming, basic]
    params:
      phone_id: {type: string, required: true}
      delay_ms: {type: integer, default: 0}
    ---
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

Doc 1 (metadata) is pure YAML — no Jinja.
Doc 2 (body) is a Jinja template that renders to YAML after param substitution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from jinja2 import Environment, StrictUndefined, UndefinedError

from src.scenario_engine.filters import register_filters


class PatternError(Exception):
    """Raised on pattern loading or instantiation errors."""


# JSONSchema for the metadata document (doc 1).
META_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name", "version", "description"],
    "properties": {
        "name": {"type": "string", "pattern": r"^[a-z][a-z0-9-]{0,63}$"},
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
        "params": {"type": "object"},
        "examples": {"type": "array"},
        "requires": {"type": "array"},
        "failure_modes": {"type": "array"},
        "compatibility": {"type": "object"},
    },
    "additionalProperties": False,
}

# JSONSchema for the body document (doc 2, after Jinja rendering).
BODY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hooks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["when"],
                "properties": {
                    "when": {"type": "string"},
                    "on_phone": {},
                    "match": {"type": "object"},
                    "save": {"type": "object"},
                    "then": {"type": "array"},
                    "once": {"type": "boolean"},
                    "ms": {},
                    "checkpoint": {"type": "string"},
                    "offset_ms": {},
                },
            },
        },
        "expected_timeline": {"type": "array"},
        "initial_actions": {"type": "array"},
    },
    "additionalProperties": True,
}


@dataclass
class PatternTemplate:
    """Loaded-but-not-yet-instantiated pattern (metadata + raw body template)."""

    path: str
    meta: dict[str, Any]
    body_template: str

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def version(self) -> str:
        return self.meta["version"]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.meta.get("description", ""),
            "tags": list(self.meta.get("tags", [])),
            "params": dict(self.meta.get("params", {})),
        }

    def full_spec(self) -> dict[str, Any]:
        """Metadata + raw body template. Callers that want rendered hooks /
        initial_actions should call `instantiate_pattern(...)` instead."""
        return {
            **self.meta,
            "path": self.path,
            "body_template": self.body_template,
        }


@dataclass
class Pattern:
    """An instantiated pattern: metadata + resolved params + rendered body."""

    name: str
    version: str
    description: str
    resolved_params: dict[str, Any]
    hooks: list[dict[str, Any]] = field(default_factory=list)
    expected_timeline: list[dict[str, Any]] = field(default_factory=list)
    initial_actions: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "resolved_params": dict(self.resolved_params),
            "hooks": [dict(h) for h in self.hooks],
            "expected_timeline": [dict(e) for e in self.expected_timeline],
            "initial_actions": [dict(a) for a in self.initial_actions],
            "tags": list(self.tags),
        }


def _split_two_docs(text: str) -> tuple[str, str]:
    """Split on first line that is exactly '---'. Returns (meta, body)."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            meta_text = "\n".join(lines[:i])
            body_text = "\n".join(lines[i + 1 :])
            return meta_text, body_text
    raise PatternError("pattern file must contain '---' separator between metadata and body")


def _resolve_params(
    params_schema: dict[str, Any],
    user_params: dict[str, Any],
) -> dict[str, Any]:
    """Apply defaults, check required, return resolved dict (without schema validation)."""
    resolved: dict[str, Any] = {}
    for pname, pspec in (params_schema or {}).items():
        if not isinstance(pspec, dict):
            raise PatternError(f"params.{pname} must be an object, got {type(pspec).__name__}")
        if pname in user_params:
            resolved[pname] = user_params[pname]
        elif "default" in pspec:
            resolved[pname] = pspec["default"]
        elif pspec.get("required"):
            raise PatternError(f"required param missing: {pname}")
    # Unknown params are passed through (permissive during MVP)
    for pname, pval in user_params.items():
        if pname not in resolved:
            resolved[pname] = pval
    return resolved


def _validate_params(params_schema: dict[str, Any], resolved: dict[str, Any]) -> None:
    """Validate resolved params against per-field JSONSchema."""
    if not params_schema:
        return
    for pname, pspec in params_schema.items():
        if pname not in resolved:
            continue
        # Strip non-JSONSchema keys (required, default)
        field_schema = {k: v for k, v in pspec.items() if k not in ("required", "default")}
        if not field_schema:
            continue
        try:
            jsonschema.validate(resolved[pname], field_schema)
        except jsonschema.ValidationError as e:
            raise PatternError(f"param {pname} validation failed: {e.message}") from e


def load_pattern_template(path: str | Path) -> PatternTemplate:
    """Read a YAML file and return an unrealised PatternTemplate."""
    path_str = str(path)
    try:
        text = Path(path_str).read_text(encoding="utf-8")
    except OSError as e:
        raise PatternError(f"cannot read pattern file {path_str}: {e}") from e
    meta_text, body_text = _split_two_docs(text)
    try:
        meta = yaml.safe_load(meta_text) or {}
    except yaml.YAMLError as e:
        raise PatternError(f"metadata YAML parse failed in {path_str}: {e}") from e
    try:
        jsonschema.validate(meta, META_SCHEMA)
    except jsonschema.ValidationError as e:
        raise PatternError(f"metadata schema validation failed in {path_str}: {e.message}") from e
    return PatternTemplate(path=path_str, meta=meta, body_template=body_text)


def _make_env() -> Environment:
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    register_filters(env)
    return env


def instantiate_pattern(
    template: PatternTemplate,
    user_params: dict[str, Any] | None = None,
) -> Pattern:
    """Render template with params, parse body YAML, return Pattern instance."""
    user_params = dict(user_params or {})
    params_schema = template.meta.get("params", {}) or {}
    resolved = _resolve_params(params_schema, user_params)
    _validate_params(params_schema, resolved)

    env = _make_env()
    try:
        rendered = env.from_string(template.body_template).render(**resolved)
    except UndefinedError as e:
        raise PatternError(
            f"pattern {template.name}: undefined variable in template: {e.message}"
        ) from e
    try:
        body = yaml.safe_load(rendered) or {}
    except yaml.YAMLError as e:
        raise PatternError(
            f"pattern {template.name}: rendered body is not valid YAML: {e}\n---\n{rendered}\n---"
        ) from e
    try:
        jsonschema.validate(body, BODY_SCHEMA)
    except jsonschema.ValidationError as e:
        raise PatternError(
            f"pattern {template.name}: body schema validation failed: {e.message}"
        ) from e

    return Pattern(
        name=template.name,
        version=template.version,
        description=template.meta.get("description", ""),
        resolved_params=resolved,
        hooks=list(body.get("hooks", []) or []),
        expected_timeline=list(body.get("expected_timeline", []) or []),
        initial_actions=list(body.get("initial_actions", []) or []),
        tags=list(template.meta.get("tags", []) or []),
    )


class PatternLoader:
    """Loads individual pattern files (stateless wrapper)."""

    def load(self, path: str | Path) -> PatternTemplate:
        return load_pattern_template(path)

    def instantiate(
        self,
        template: PatternTemplate,
        params: dict[str, Any] | None = None,
    ) -> Pattern:
        return instantiate_pattern(template, params)


class PatternRegistry:
    """Manages a directory-wide catalog of pattern templates."""

    def __init__(self, patterns_dir: str | Path) -> None:
        self._dir = Path(patterns_dir)
        self._templates: dict[str, PatternTemplate] = {}
        self._errors: dict[str, str] = {}

    def scan(self) -> None:
        """(Re)load all patterns from the directory."""
        self._templates.clear()
        self._errors.clear()
        if not self._dir.exists():
            return
        for yaml_file in sorted(self._dir.glob("*.yaml")):
            try:
                tmpl = load_pattern_template(yaml_file)
                self._templates[tmpl.name] = tmpl
            except PatternError as e:
                self._errors[str(yaml_file)] = str(e)

    def list(
        self,
        tags: list[str] | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        q = query.lower() if query else None
        tag_set = set(tags) if tags else None
        for tmpl in self._templates.values():
            meta = tmpl.meta
            if tag_set and not tag_set & set(meta.get("tags", [])):
                continue
            if q:
                hay = f"{tmpl.name} {meta.get('description', '')}".lower()
                if q not in hay:
                    continue
            result.append(tmpl.summary())
        return sorted(result, key=lambda r: r["name"])

    def get_template(self, name: str) -> PatternTemplate:
        if name not in self._templates:
            raise PatternError(f"pattern not found: {name}")
        return self._templates[name]

    def get_full_spec(self, name: str) -> dict[str, Any]:
        tmpl = self.get_template(name)
        return tmpl.full_spec()

    def instantiate(self, name: str, params: dict[str, Any] | None = None) -> Pattern:
        tmpl = self.get_template(name)
        return instantiate_pattern(tmpl, params)

    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def names(self) -> list[str]:
        return sorted(self._templates.keys())
