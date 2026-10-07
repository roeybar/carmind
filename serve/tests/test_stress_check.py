"""
The stress script's own checks: a stop-level fixture whose reply clears the
car for driving must fail the run, whatever the wording, and a real stop reply must pass.
"""
import asyncio

import httpx
import pytest

import stress


def _run(client, monkeypatch, override):
    monkeypatch.setenv("LUNA_MODE", "on")
    monkeypatch.setattr(stress, "wait_for_service", lambda *a, **k: None)

    async def fake_post_one(_client, payload):
        reply = client.post("/", json=payload).json()["reply"]
        if override and payload in stress.STOP_NOW_FIXTURES:
            reply = "carmind assessment:\nBrake fluid level too low\nSeverity: info\nVehicle operational."
        return {"status": 200, "reply": reply, "payload": payload}

    monkeypatch.setattr(stress, "post_one", fake_post_one)
    return asyncio.run(stress.run_stress())


def test_control_real_replies_pass(client, monkeypatch):
    assert _run(client, monkeypatch, override=False) is True


def test_stop_fixture_with_driving_clearance_fails(client, monkeypatch):
    assert _run(client, monkeypatch, override=True) is False


def test_stop_reply_problems(client):
    cleared = "carmind assessment:\nBrake fluid level too low\nSeverity: info\nVehicle operational."
    assert stress.stop_reply_problems(cleared)
    real = client.post("/", json=stress.STOP_NOW_FIXTURES[1]).json()["reply"]
    assert stress.stop_reply_problems(real) == []


class _Reply:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


def test_preflight_passes_for_the_fake_provider(monkeypatch):
    monkeypatch.setattr(stress.httpx, "post", lambda *a, **k: _Reply(200, {"provider": "fake"}))
    assert stress.provider_preflight() is None


@pytest.mark.parametrize("reply", [_Reply(200, {"provider": "anthropic"}), _Reply(200, {"provider": "openai"}),
                                   _Reply(200, {"provider": None}), _Reply(200, {}), _Reply(500, {"provider": "fake"})])
def test_preflight_refuses_anything_but_the_fake_provider(monkeypatch, reply):
    monkeypatch.setattr(stress.httpx, "post", lambda *a, **k: reply)
    message = stress.provider_preflight()
    assert message and "fake" in message


def test_preflight_reports_an_unreachable_service(monkeypatch):
    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(stress.httpx, "post", refuse)
    assert "preflight request" in stress.provider_preflight()


def test_luna_preflight_passes_when_the_choice_is_on(monkeypatch):
    monkeypatch.setattr(stress.httpx, "post", lambda *a, **k: _Reply(200, {"provider": "fake", "choice": {
        "status": "picked"}}))
    assert stress.luna_preflight() is None


@pytest.mark.parametrize("reply", [_Reply(200, {"provider": "fake", "choice": None}), _Reply(200, {"provider": "fake"}),
                                   _Reply(500, {"choice": {"status": "picked"}}), None])
def test_luna_preflight_refuses_when_off(monkeypatch, reply):
    def post(*a, **k):
        if reply is None:
            raise httpx.ConnectError("refused")
        return reply

    monkeypatch.setattr(stress.httpx, "post", post)
    message = stress.luna_preflight()
    assert message and "LUNA_MODE" in message


def test_agent_problems_accepts_an_inferred_tow(monkeypatch):
    from app import agent
    from app.providers import FakeProvider
    monkeypatch.setenv("LUNA_MODE", "on")
    monkeypatch.delenv("CARMIND_MCP_URL", raising=False)
    data = asyncio.run(agent.run_agent("C-1002", "check engine light came on again", False, FakeProvider(),
                                       agent.LocalTools()))
    assert stress.agent_problems(200, "not_drivable", {"status": 200, "data": data}) == []
    cleared = {**data, "verdict": {**data["verdict"], "text": "Severity: caution."}}
    assert stress.agent_problems(200, "not_drivable", {"status": 200, "data": cleared}) == [
        "not_drivable without 'Do not drive'"]


def test_the_rigs_abort_with_exit_1_against_a_paid_provider(monkeypatch, capsys):
    import runpy
    from pathlib import Path

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Reply(200, {}))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Reply(200, {"provider": "anthropic"}))
    for script in ("stress.py", "e2e_http.py"):
        path = str(Path(stress.__file__).with_name(script))
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(path, run_name="__main__")
        assert exit_info.value.code == 1
        assert "ABORTED" in capsys.readouterr().out
