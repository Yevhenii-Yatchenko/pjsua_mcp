"""Integration test for the hello-world scenario against live Asterisk.

Requires docker-compose.test.yml stack (Asterisk on sipnet + test-runner
container with pjsua2 built). Run via:

    docker compose -f docker-compose.test.yml run --rm test-runner \\
        pytest tests/scenarios/ -v -m integration

Marked `integration` so it is skipped by default.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def asterisk_config() -> dict:
    """Load Asterisk creds from env (see docker-compose.test.yml)."""
    return {
        "domain": os.environ.get("SIP_DOMAIN", "asterisk"),
        "user_a": os.environ.get("SIP_USER_A", "6001"),
        "pass_a": os.environ.get("SIP_PASS_A", "test123"),
        "user_b": os.environ.get("SIP_USER_B", "6002"),
        "pass_b": os.environ.get("SIP_PASS_B", "test123"),
    }


def test_hello_world_scenario_against_asterisk(asterisk_config: dict) -> None:
    """End-to-end: provision A/B, run hello-world, verify terminal timeline."""
    from src.sip_engine import SipEngine
    from src.account_manager import PhoneRegistry
    from src.call_manager import CallManager
    from src.pcap_manager import PcapManager
    from src.scenario_engine.event_bus import EventBus, set_default_bus
    from src.scenario_engine.pattern_loader import PatternRegistry
    from src.scenario_engine.orchestrator import Scenario, ScenarioRunner

    async def run() -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop=loop)
        set_default_bus(bus)
        try:
            engine = SipEngine()
            engine.initialize()
            registry = PhoneRegistry(engine)
            pcap_mgr = PcapManager()
            cm = CallManager(engine, registry, pcap_mgr=pcap_mgr)

            registry.add_phone(
                "a",
                domain=asterisk_config["domain"],
                username=asterisk_config["user_a"],
                password=asterisk_config["pass_a"],
                register=True,
            )
            registry.add_phone(
                "b",
                domain=asterisk_config["domain"],
                username=asterisk_config["user_b"],
                password=asterisk_config["pass_b"],
                register=True,
            )

            # Pump pjsip events in background
            stop_poll = False

            async def pump() -> None:
                while not stop_poll:
                    engine.handle_events(10)
                    await asyncio.sleep(0.02)

            pump_task = asyncio.create_task(pump())

            try:
                patterns = PatternRegistry("scenarios/patterns")
                patterns.scan()
                assert not patterns.errors(), patterns.errors()

                runner = ScenarioRunner(
                    bus=bus,
                    pattern_registry=patterns,
                    call_manager=cm,
                    registry=registry,
                    loop=loop,
                )
                dest = f"sip:{asterisk_config['user_b']}@{asterisk_config['domain']}"
                scenario = Scenario(
                    name="hello-world-integration",
                    phones=["a", "b"],
                    patterns=[
                        {"use": "wait-for-registration", "phone_id": "a", "timeout_ms": 5000},
                        {"use": "wait-for-registration", "phone_id": "b", "timeout_ms": 5000},
                        {"use": "auto-answer", "phone_id": "b", "delay_ms": 300},
                        {"use": "send-dtmf-on-confirmed", "phone_id": "a", "digits": "1", "initial_delay_ms": 200},
                        {"use": "hangup-after-duration", "phone_id": "a", "duration_ms": 3000},
                        {"use": "make-call-and-wait-confirmed", "phone_id": "a", "dest_uri": dest, "timeout_ms": 10000},
                    ],
                    stop_on=[{"phone_id": "a", "event": "call.state.disconnected"}],
                    timeout_ms=20000,
                )
                result = await runner.run(scenario)
                assert result.status == "ok", (
                    f"scenario status={result.status} reason={result.reason}\n"
                    f"errors={result.errors}"
                )
                # Must have both reg.success events
                reg_events = [e for e in result.timeline if e["type"] == "reg.success"]
                assert {e["phone_id"] for e in reg_events} == {"a", "b"}
                # Must have B's incoming and A's confirmed
                assert any(
                    e["type"] == "call.state.incoming" and e["phone_id"] == "b"
                    for e in result.timeline
                )
                assert any(
                    e["type"] == "call.state.confirmed" and e["phone_id"] == "a"
                    for e in result.timeline
                )
                # Must have the answer, DTMF and hangup actions
                action_types = {e["type"] for e in result.timeline if e["kind"] == "action"}
                assert "answer" in action_types
                assert "send_dtmf" in action_types
                assert "hangup" in action_types
            finally:
                stop_poll = True
                pump_task.cancel()
                try:
                    await pump_task
                except asyncio.CancelledError:
                    pass
                cm.hangup_all()
                registry.drop_all()
                engine.shutdown()
        finally:
            set_default_bus(None)

    asyncio.run(run())
