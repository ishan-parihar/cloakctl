"""Unit tests for the MCP server (mcp_server.py), mcp 2.x MCPServer.

No browser needed: verifies tool registration, flat schemas with Field
constraints, delegation to the same core ops the CLI uses, and that core
failures surface as tool-level error text (is_error), never tracebacks.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from cloakctl import paths  # noqa: E402


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOAKCTL_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(paths, "HOME_STATE", tmp_path / "state")
    monkeypatch.setattr(paths, "PROFILES_DIR", tmp_path / "state" / "profiles")
    monkeypatch.setattr(paths, "RUNTIME_DIR", tmp_path / "state" / "runtime")
    yield tmp_path / "state"


def _run(coro):
    return asyncio.run(coro)


async def _tools():
    from cloakctl.mcp_server import mcp

    return {t.name: t for t in await mcp.list_tools()}


def test_tools_registered_with_service_prefix(state):
    tools = _run(_tools())
    assert all(n.startswith("cloakctl_") for n in tools), sorted(tools)
    for expected in ("cloakctl_open", "cloakctl_close", "cloakctl_navigate",
                     "cloakctl_snapshot", "cloakctl_read", "cloakctl_act",
                     "cloakctl_exec", "cloakctl_run", "cloakctl_wf_save",
                     "cloakctl_wf_run", "cloakctl_doctor", "cloakctl_status",
                     "cloakctl_validate", "cloakctl_profile_create"):
        assert expected in tools, f"missing {expected}"


def test_schemas_flat_and_constrained(state):
    """Flat signatures + Field constraints land in the JSON schema."""
    tools = _run(_tools())
    schema = tools["cloakctl_open"].input_schema
    props = schema.get("properties", {})
    assert props["profile"]["type"] == "string"
    assert props["profile"]["minLength"] == 1
    assert schema.get("required") == ["profile"]
    # read-only tools carry the annotation
    assert tools["cloakctl_doctor"].annotations.read_only_hint is True
    ann = tools["cloakctl_close"].annotations
    assert ann is None or ann.read_only_hint is not True


async def _call(name: str, args: dict):
    from cloakctl.mcp_server import mcp

    res = await mcp.call_tool(name, args)
    return res


def test_doctor_delegates_to_core(state):
    res = _run(_call("cloakctl_doctor", {}))
    text = res.content[0].text
    out = json.loads(text)
    assert out["defaultEngine"] in ("obscura", "cloakbrowser")
    assert res.is_error is False


def test_open_ghost_is_tool_error_with_fix_hint(state):
    res = _run(_call("cloakctl_open", {"profile": "ghost"}))
    text = res.content[0].text
    assert res.is_error is True
    assert "error:" in text
    assert "cloakctl_profile_create" in text  # self-correcting hint


def test_navigate_unopen_profile_is_tool_error(state):
    paths.profile_dir("nav").mkdir(parents=True, exist_ok=True)
    res = _run(_call("cloakctl_navigate",
                     {"profile": "nav", "url": "http://127.0.0.1:1/"}))
    text = res.content[0].text
    assert res.is_error is True
    assert "cloakctl_open" in text


def test_profile_create_idempotent_ack(state):
    r1 = _run(_call("cloakctl_profile_create", {"profile": "p"}))
    r2 = _run(_call("cloakctl_profile_create", {"profile": "p"}))
    o1 = json.loads(r1.content[0].text)
    o2 = json.loads(r2.content[0].text)
    assert o1["alreadyExisted"] is False and o2["alreadyExisted"] is True
    assert paths.profile_dir("p").exists()


def test_entrypoint_exists(state):
    import cloakctl.mcp_server as m

    assert callable(m.main)
