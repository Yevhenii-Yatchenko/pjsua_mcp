"""Shared pytest fixtures for PJSUA MCP tests."""

from __future__ import annotations

import pytest
import pjsua2 as pj


@pytest.fixture(scope="session")
def pjsua_endpoint():
    """Session-scoped live PJSUA2 endpoint (no network, null audio)."""
    ep = pj.Endpoint()
    ep.libCreate()

    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 5
    ep_cfg.logConfig.consoleLevel = 5
    ep_cfg.uaConfig.threadCnt = 0
    ep_cfg.uaConfig.mainThreadOnly = True

    ep.libInit(ep_cfg)
    ep.audDevManager().setNullDev()
    ep.libStart()

    yield ep

    ep.libDestroy()


@pytest.fixture()
def tmp_captures_dir(tmp_path, monkeypatch):
    """Patch pcap_manager CAPTURES_ROOT/CAPTURES_DIR to a temp directory."""
    import src.pcap_manager as pm
    monkeypatch.setattr(pm, "CAPTURES_ROOT", tmp_path)
    monkeypatch.setattr(pm, "CAPTURES_DIR", tmp_path)
    return tmp_path
