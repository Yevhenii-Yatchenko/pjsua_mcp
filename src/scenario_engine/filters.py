"""Custom Jinja filters for pattern YAML rendering."""

from __future__ import annotations

import re
from typing import Any


def sip_user(uri: str) -> str:
    """Extract user part from SIP URI: 'sip:1001@domain' -> '1001'."""
    m = re.match(r"sips?:([^@;>]+)", uri or "")
    return m.group(1) if m else ""


def sip_host(uri: str) -> str:
    """Extract host part from SIP URI: 'sip:1001@domain:5060' -> 'domain:5060'."""
    m = re.match(r"sips?:[^@]+@([^;>]+)", uri or "")
    return m.group(1) if m else ""


def ms_to_sec(ms: Any) -> float:
    return float(ms) / 1000.0


def as_int(value: Any) -> int:
    return int(value)


def as_str(value: Any) -> str:
    return str(value)


def register_filters(env: Any) -> None:
    """Register all custom filters on a Jinja Environment."""
    env.filters["sip_user"] = sip_user
    env.filters["sip_host"] = sip_host
    env.filters["ms_to_sec"] = ms_to_sec
    env.filters["as_int"] = as_int
    env.filters["as_str"] = as_str
