"""
The MCP server in memory: the four desk tools are listed and callable, errors are flagged.
"""
import asyncio
import json

from mcp.shared.memory import create_connected_server_and_client_session

from app import desk, mcp_probe, mcp_server


def _with_session(fn):
    async def run():
        async with create_connected_server_and_client_session(mcp_server.server._mcp_server) as session:
            return await fn(session)
    return asyncio.run(run())


def test_lists_the_four_desk_tools():
    result = _with_session(lambda s: s.list_tools())
    assert {t.name for t in result.tools} == set(desk.TOOLS)
    assert all(t.description for t in result.tools)


def test_assess_warning_light_returns_the_verdict_as_json():
    result = _with_session(lambda s: s.call_tool(
        "assess_warning_light", {"customer_id": "C-1001", "description": "red brake warning light"}))
    assert not result.isError
    data = json.loads(result.content[0].text)
    assert data["status"] == "not_drivable"
    assert data["severity"] == "stop_now"


def test_light_is_an_optional_string_on_assess_only():
    tools = {t.name: t.inputSchema for t in _with_session(lambda s: s.list_tools()).tools}
    assert set(tools["assess_warning_light"]["properties"]) == {
        "customer_id", "description", "after_hours", "caller_number", "plate", "light"}
    assert tools["assess_warning_light"]["properties"]["light"]["type"] == "string"
    assert "light" not in tools["assess_warning_light"].get("required", [])
    assert set(tools["plan_dispatch"]["properties"]) == {
        "customer_id", "description", "after_hours", "caller_number", "plate"}
    assert set(tools["get_customer"]["properties"]) == {"customer_id", "caller_number", "plate"}
    assert set(tools["get_company_procedure"]["properties"]) == {"situation"}


def test_assess_over_mcp_takes_the_light():
    result = _with_session(lambda s: s.call_tool("assess_warning_light", {
        "customer_id": "C-1001", "description": "coolant light and check engine light both on",
        "light": "coolant_level_low"}))
    assert not result.isError
    data = json.loads(result.content[0].text)
    assert (data["status"], data["indicator"]) == ("drivable_caution", "coolant_level_low")


def test_unknown_tool_and_missing_argument_are_errors():
    assert _with_session(lambda s: s.call_tool("no_such_tool", {})).isError
    assert _with_session(lambda s: s.call_tool("get_customer", {})).isError


def _probe_returning(monkeypatch, result):
    async def fake_probe(url, customer_id, description):
        return {"tools": sorted(desk.TOOLS), "result": result}

    monkeypatch.setattr(mcp_probe, "probe", fake_probe)
    return mcp_probe.main(["--wait", "0"])


def test_probe_passes_a_result_with_a_status(monkeypatch, capsys):
    assert _probe_returning(monkeypatch, {"status": "not_drivable", "severity": "stop_now"}) == 0
    assert "PASS mcp call assess_warning_light" in capsys.readouterr().out


def test_probe_fails_a_result_without_a_status(monkeypatch, capsys):
    assert _probe_returning(monkeypatch, {"error": "unknown_customer"}) == 1
    out = capsys.readouterr().out
    assert "FAIL mcp call" in out
    assert "PASS mcp call" not in out
