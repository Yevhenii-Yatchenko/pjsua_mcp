"""Hook runtime — arms hooks on event bus, matches events, triggers actions.

A Hook represents one entry in a pattern's `hooks:` list. When an event matches
(type + on_phone + match-predicates), the runtime schedules the `then` actions
on the scenario's asyncio loop via ActionExecutor.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from src.scenario_engine.event_bus import Event, EventBus


@dataclass
class Hook:
    """One armed hook — `when` + `on_phone` filter + `then` actions."""

    hook_id: str
    when: list[str]
    on_phone: str | None
    match: dict[str, Any]
    then: list[Any]
    once: bool
    pattern_name: str
    sub_id: int | None = None


def _dotted_get(obj: Any, path: str) -> Any:
    """Get value from nested dict via dotted path, e.g. 'headers.Refer-To'."""
    current = obj
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


def _value_matches(expected: Any, actual: Any) -> bool:
    """Compare an expected match value against actual event field.

    Supported:
      exact equality (string/number/bool)
      regex string starting with `~` — e.g. "~^Q\\.850"
      list — membership
      "4xx" — status code class (400-499)
      "5xx" — status code class (500-599)
    """
    if expected is None:
        return actual is None
    if isinstance(expected, list):
        return actual in expected
    if isinstance(expected, str):
        if expected.startswith("~"):
            pattern = expected[1:]
            if actual is None:
                return False
            return bool(re.search(pattern, str(actual)))
        if re.fullmatch(r"[1-6]xx", expected):
            try:
                code = int(actual)
            except (TypeError, ValueError):
                return False
            low = int(expected[0]) * 100
            return low <= code < low + 100
    return expected == actual


def _event_matches_predicates(event: Event, match: dict[str, Any]) -> bool:
    """Check if an event satisfies all key=value predicates in `match`."""
    if not match:
        return True
    for key, expected in match.items():
        # First try event.data, then fall back to top-level attributes.
        actual = _dotted_get(event.data, key)
        if actual is None:
            actual = getattr(event, key, None)
        if not _value_matches(expected, actual):
            return False
    return True


def _hook_matches(hook: Hook, event: Event) -> bool:
    # Type match (patterns check is done at subscribe time, but we re-verify here
    # because subscribe uses the raw patterns which may include wildcards).
    if hook.on_phone is not None and event.phone_id is not None:
        if hook.on_phone != event.phone_id:
            return False
    elif hook.on_phone is not None and event.phone_id is None:
        return False
    return _event_matches_predicates(event, hook.match)


# Action executor is passed in as a callable — see action_executor.py
ActionExecutorFn = Callable[[list[Any], Hook, Event], Awaitable[None]]


class HookRuntime:
    """Manages armed hooks, subscribes to bus, dispatches matched events to actions."""

    def __init__(
        self,
        bus: EventBus,
        action_executor: ActionExecutorFn,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._bus = bus
        self._exec = action_executor
        self._loop = loop
        self._hooks: dict[str, Hook] = {}

    def arm(
        self,
        spec: dict[str, Any],
        pattern_name: str = "<inline>",
    ) -> Hook:
        """Arm a single hook from a pattern spec dict (one entry of `hooks:` list)."""
        when_raw = spec.get("when")
        if isinstance(when_raw, str):
            when = [when_raw]
        elif isinstance(when_raw, list):
            when = list(when_raw)
        else:
            raise ValueError(f"hook `when` must be string or list, got {type(when_raw).__name__}")

        on_phone_raw = spec.get("on_phone")
        on_phone = None if on_phone_raw in (None, "") else str(on_phone_raw)

        hook = Hook(
            hook_id=str(uuid.uuid4()),
            when=when,
            on_phone=on_phone,
            match=dict(spec.get("match") or {}),
            then=list(spec.get("then") or []),
            once=bool(spec.get("once", True)),
            pattern_name=pattern_name,
        )

        def callback(event: Event) -> None:
            if not _hook_matches(hook, event):
                return
            # If once=True, detach AFTER a confirmed match (not after the
            # first event of the subscribed type — that could be a mismatch).
            if hook.once and hook.sub_id is not None:
                self._bus.unsubscribe(hook.sub_id)
                hook.sub_id = None
            # Schedule actions on scenario loop.
            coro = self._exec(hook.then, hook, event)
            asyncio.run_coroutine_threadsafe(coro, self._loop)

        # Always subscribe with bus-level once=False; hook-level `once` is
        # handled inside the callback so non-matching events do NOT consume
        # the subscription.
        sub_id = self._bus.subscribe(when, callback, once=False)
        hook.sub_id = sub_id
        self._hooks[hook.hook_id] = hook
        return hook

    def remove(self, hook_id: str) -> None:
        hook = self._hooks.pop(hook_id, None)
        if hook is None:
            return
        if hook.sub_id is not None:
            self._bus.unsubscribe(hook.sub_id)

    def remove_all(self) -> None:
        for hid in list(self._hooks.keys()):
            self.remove(hid)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "hook_id": h.hook_id,
                "when": list(h.when),
                "on_phone": h.on_phone,
                "match": dict(h.match),
                "once": h.once,
                "pattern_name": h.pattern_name,
            }
            for h in self._hooks.values()
        ]
