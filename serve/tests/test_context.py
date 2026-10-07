"""
Query-time context flags (docs/resolution-order.md step 2).

A request may carry "context_flags". A context rule whose flags are all present can
move a drivable verdict to a not-drivable one (tow or escalate), and can keep the LLM
out of an ambiguous query. It never changes a verdict that is already not drivable,
and never makes one less cautious.
"""
import itertools
import json
from pathlib import Path

import pytest

import app.luna as luna
from app import kb, tools

ALL_FLAGS = ["repeat_problem", "recent_long_service", "near_service_interval",
             "fresh_handover", "no_pre_delivery_test", "after_service_hours"]

TOW_EN = "Tow recommended - do not drive."
TOW_HE = "יש לגרור את הרכב - אין לנסוע."

DRIVABLE_TEXTS = [
    ("coolant level low", "Conditional - check first before driving."),
    ("check engine light", "Drive with caution."),
    ("esc stability control light is flashing", "Vehicle operational."),
]


def _post(client, text, flags=None, language="en-US", plate="FH-104-27"):
    body = {"language": language, "messages": [{"role": "user", "content": text}], "plate": plate}
    if flags is not None:
        body["context_flags"] = flags
    resp = client.post("/", json=body)
    assert resp.status_code == 200
    return resp.json()["reply"]


def _rule(rule_id, when_flags, then, when_symptom=None):
    rule = {"id": rule_id, "when_flags": when_flags, "then": then, "rationale_en": "test rule"}
    if when_symptom:
        rule["when_symptom"] = when_symptom
    return rule


@pytest.mark.parametrize("text,base_line", DRIVABLE_TEXTS)
def test_repeat_problem_turns_drivable_verdict_into_tow(client, text, base_line):
    lines = _post(client, text, ["repeat_problem"]).splitlines()
    assert TOW_EN in lines, lines
    assert base_line not in lines, lines
    assert "Context: repeat_problem" in lines, lines


def test_context_keeps_manufacturer_severity(client):
    lines = _post(client, "check engine light", ["repeat_problem"]).splitlines()
    assert "Severity: caution" in lines, lines


def test_tow_in_hebrew(client):
    reply = _post(client, "נורת מנוע דלוקה", ["repeat_problem"], language="he-IL")
    assert TOW_HE in reply.splitlines(), reply
    assert "הקשר: repeat_problem" in reply.splitlines(), reply


def test_stop_now_is_not_changed_by_context(client):
    base = _post(client, "brake fluid warning")
    assert _post(client, "brake fluid warning", ALL_FLAGS) == base


@pytest.mark.parametrize("flag", [f for f in ALL_FLAGS if f != "repeat_problem"])
def test_flag_without_a_rule_changes_nothing(client, flag):
    for text, _ in DRIVABLE_TEXTS:
        assert _post(client, text, [flag]) == _post(client, text)


def test_unknown_flag_is_refused_with_error_reply(client):
    reply = _post(client, "check engine light", ["all_clear"])
    assert reply.startswith("An error occurred while processing the request."), reply


def test_flags_must_be_a_list(client):
    reply = _post(client, "check engine light", "repeat_problem")
    assert reply.startswith("An error occurred while processing the request."), reply


def test_ambiguous_query_with_fired_rule_keeps_llm_out(client, monkeypatch):
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or luna.Outcome("picked", "engine_control_petrol", "r"))
    reply = _post(client, "coolant light and check engine light both on", ["repeat_problem"])
    assert calls == [], f"LLM consulted although a context rule fired: {calls}"
    assert reply.startswith("Multiple possible warning lights"), reply
    assert "do not drive" in reply.lower(), reply


def test_ambiguous_query_without_flags_still_reaches_llm(client, monkeypatch):
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or None)
    _post(client, "coolant light and check engine light both on", [])
    assert len(calls) == 1


def test_escalate_rule_gives_escalate_verdict(client, synthetic_kb):
    synthetic_kb(context_rules=[_rule("fh", ["fresh_handover"], "escalate")])
    lines = _post(client, "check engine light", ["fresh_handover"]).splitlines()
    assert "Contact a qualified specialist." in lines, lines
    assert "Drive with caution." not in lines, lines


def test_rule_needs_all_its_flags(client, synthetic_kb):
    synthetic_kb(context_rules=[_rule("both", ["recent_long_service", "near_service_interval"], "tow")])
    assert TOW_EN not in _post(client, "check engine light", ["recent_long_service"])
    lines = _post(client, "check engine light",
                  ["recent_long_service", "near_service_interval"]).splitlines()
    assert TOW_EN in lines, lines
    assert "Context: near_service_interval, recent_long_service" in lines, lines


def test_rule_without_flags_never_fires(client, synthetic_kb):
    synthetic_kb(context_rules=[_rule("empty", [], "tow")])
    for flags in ([], ALL_FLAGS):
        lines = _post(client, "check engine light", flags).splitlines()
        assert "Drive with caution." in lines and TOW_EN not in lines, (flags, lines)


def test_rule_scoped_to_one_indicator(client, synthetic_kb):
    synthetic_kb(context_rules=[_rule("cool", ["fresh_handover"], "tow", "coolant_level_low")])
    assert TOW_EN in _post(client, "coolant level low", ["fresh_handover"]).splitlines()
    assert TOW_EN not in _post(client, "check engine light", ["fresh_handover"])


def test_rule_scoped_to_one_candidate_keeps_llm_out(client, synthetic_kb, monkeypatch):
    synthetic_kb(context_rules=[_rule("cool", ["fresh_handover"], "tow", "coolant_level_low")])
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or None)
    reply = _post(client, "coolant light and check engine light both on", ["fresh_handover"])
    assert calls == [] and "do not drive" in reply.lower(), (calls, reply)


def test_raise_conservatism_only_affects_ambiguous_queries(client, synthetic_kb, monkeypatch):
    synthetic_kb(context_rules=[_rule("rc", ["after_service_hours"], "raise_conservatism")])
    assert _post(client, "check engine light", ["after_service_hours"]) == _post(client, "check engine light")
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or luna.Outcome("picked", "engine_control_petrol", "r"))
    reply = _post(client, "coolant light and check engine light both on", ["after_service_hours"])
    assert calls == [] and "do not drive" in reply.lower(), reply


_STATUS_RANK = {"drivable": 0, "drivable_caution": 1, "not_drivable": 2}


def _subsets(items):
    return [list(c) for n in range(len(items) + 1) for c in itertools.combinations(items, n)]


@pytest.mark.parametrize("then", ["tow", "escalate", "raise_conservatism"])
def test_context_never_loosens_a_verdict(monkeypatch, real_indicators, then):
    from app import orchestrator
    no_severity = dict(real_indicators[0], id="skoda_gas.x", severity=None, hard_stop=False)
    records = real_indicators + [no_severity]
    rules = [_rule(f"r{i}", [flag], then) for i, flag in enumerate(ALL_FLAGS)]
    monkeypatch.setattr(kb, "context_rules", lambda: rules)
    inferable = _subsets(orchestrator.INFERABLE_FLAGS)
    for rec, n in itertools.product(records, range(len(ALL_FLAGS) + 1)):
        base = tools.drivability_assessment(rec)
        bare = rec["id"].split(".")[-1]
        for flags in itertools.combinations(ALL_FLAGS, n):
            after = tools.apply_context(base, bare, list(flags))
            assert _STATUS_RANK[after["status"]] >= _STATUS_RANK[base["status"]], (rec["id"], flags)
            if base["status"] == "not_drivable":
                assert after == base, (rec["id"], flags)
            assert after["severity"] == base["severity"]
            for inferred in inferable:
                outcome = luna.ManyOutcome("chosen", tuple(inferred)) if inferred else None
                tightened = tools.apply_context(base, bare, orchestrator.with_inferred(list(flags), outcome))
                assert _STATUS_RANK[tightened["status"]] >= _STATUS_RANK[after["status"]], (rec["id"], flags, inferred)
                if base["status"] == "not_drivable":
                    assert tightened == base, (rec["id"], flags, inferred)
                assert tightened["severity"] == base["severity"]


def test_with_inferred_keeps_request_flags():
    from app.orchestrator import with_inferred
    assert with_inferred(["after_service_hours"], luna.ManyOutcome("chosen", ("repeat_problem",))) == [
        "after_service_hours", "repeat_problem"]
    for outcome in (luna.ManyOutcome("none"), luna.ManyOutcome("failed"), luna.ManyOutcome("rejected"), None):
        assert with_inferred(["after_service_hours"], outcome) == ["after_service_hours"], outcome


LF_EN = "check engine light came on again"


def test_request_and_inferred_flags_tow_together(client, synthetic_kb, monkeypatch):
    from app.compose import contract_flags_line
    synthetic_kb(context_rules=[{"id": "both", "when_flags": ["after_service_hours", "repeat_problem"],
                                 "then": "tow", "rationale_en": "t"}])
    monkeypatch.setenv("LUNA_MODE", "on")
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")
    reply = _post(client, LF_EN, ["after_service_hours"])
    lines = reply.splitlines()
    assert TOW_EN in lines, reply
    assert "Context: after_service_hours, repeat_problem" in lines, reply
    assert reply.split("\n\n")[-1] == contract_flags_line(["repeat_problem"], "en-US")


def test_no_inference_when_a_rule_already_fired(client, synthetic_kb, monkeypatch):
    import asyncio

    from app import agent, providers

    class Recorder(providers.FakeProvider):
        name = "scripted"

        def __init__(self):
            self.calls = []

        async def choose(self, request):
            self.calls.append("choose")
            return request.options[0].id, "r"

        async def choose_many(self, request):
            self.calls.append("choose_many")
            return [o.id for o in request.options], "r"

    synthetic_kb(context_rules=[
        {"id": "rc", "when_flags": ["after_service_hours"], "then": "raise_conservatism", "rationale_en": "t"},
        {"id": "rp", "when_flags": ["repeat_problem"], "then": "tow", "rationale_en": "t"}])
    monkeypatch.delenv("CARMIND_MCP_URL", raising=False)
    recorder = Recorder()
    monkeypatch.setattr(providers, "get_provider", lambda *a, **k: recorder)

    def both_routes():
        reply = _post(client, "check engine light", ["after_service_hours"])
        out = asyncio.run(agent.run_agent("C-1001", "check engine light", True, recorder, agent.LocalTools()))
        return reply, out["reply"]

    monkeypatch.setenv("LUNA_MODE", "off")
    off = both_routes()
    monkeypatch.setenv("LUNA_MODE", "on")
    on = both_routes()
    assert recorder.calls == []
    assert on == off


def test_request_flag_set_matches_schema():
    from typing import get_args

    from app.models import ContextFlag
    schema_path = Path(kb._resolve_root()) / "schema" / "knowledge.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert sorted(get_args(ContextFlag)) == sorted(schema["$defs"]["context_flag"]["enum"])
