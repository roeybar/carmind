"""
Knowledge records are validated against schema/knowledge.schema.json when they load.

A record that breaks the schema (for example, one with no manual_id or source_page)
stops the service from starting, and a request that reaches an invalid KB gets the
error escalate reply rather than an answer built from it.
"""
import pytest
from fastapi.testclient import TestClient

from app import kb


def _drop_provenance_field(records, field):
    del records[0]["provenance"][field]
    return records


@pytest.mark.parametrize("field", ["manual_id", "source_page"])
def test_record_missing_provenance_field_is_rejected(synthetic_kb, real_indicators, field):
    synthetic_kb(indicators=_drop_provenance_field(real_indicators, field))
    with pytest.raises(RuntimeError, match=field) as err:
        kb.indicators()
    assert "skoda_gas.brake_fluid_low" in str(err.value)


@pytest.mark.parametrize("bad_page", [0, "127"])
def test_record_with_invalid_source_page_is_rejected(synthetic_kb, real_indicators, bad_page):
    real_indicators[0]["provenance"]["source_page"] = bad_page
    synthetic_kb(indicators=real_indicators)
    with pytest.raises(RuntimeError, match="skoda_gas.brake_fluid_low"):
        kb.indicators()


def test_record_without_provenance_is_rejected(synthetic_kb, real_indicators):
    del real_indicators[2]["provenance"]
    synthetic_kb(indicators=real_indicators)
    with pytest.raises(RuntimeError, match="skoda_gas.coolant_level_low"):
        kb.indicators()


def test_record_with_unknown_severity_is_rejected(synthetic_kb, real_indicators):
    real_indicators[3]["severity"] = "unknown"
    synthetic_kb(indicators=real_indicators)
    with pytest.raises(RuntimeError, match="skoda_gas.engine_control_petrol"):
        kb.indicators()


@pytest.mark.parametrize("field,value", [("then", "drive_ok"), ("when_flags", ["all_clear"])])
def test_invalid_context_rule_is_rejected(synthetic_kb, field, value):
    rule = {"id": "bad-rule", "when_flags": ["repeat_problem"], "then": "tow", "rationale_en": "x"}
    rule[field] = value
    synthetic_kb(context_rules=[rule])
    with pytest.raises(RuntimeError, match="bad-rule"):
        kb.indicators()


def test_missing_schema_file_is_rejected(synthetic_kb):
    root = synthetic_kb()
    (root / "schema" / "knowledge.schema.json").unlink()
    with pytest.raises(RuntimeError, match="knowledge.schema.json"):
        kb.indicators()


def test_service_refuses_to_start_on_invalid_record(synthetic_kb, real_indicators):
    from app.main import app
    synthetic_kb(indicators=_drop_provenance_field(real_indicators, "manual_id"))
    with pytest.raises(RuntimeError, match="manual_id"):
        with TestClient(app):
            pass


def test_request_against_invalid_kb_gets_error_reply(client, synthetic_kb, real_indicators):
    synthetic_kb(indicators=_drop_provenance_field(real_indicators, "source_page"))
    resp = client.post("/", json={
        "language": "en-US",
        "messages": [{"role": "user", "content": "brake fluid warning"}],
    })
    assert resp.status_code == 200
    reply = resp.json()["reply"]
    assert reply.startswith("An error occurred while processing the request."), reply


def test_shipped_kb_is_valid(synthetic_kb):
    synthetic_kb()
    assert len(kb.indicators()) == 6


def test_record_with_empty_manual_id_is_rejected(synthetic_kb, real_indicators):
    real_indicators[0]["provenance"]["manual_id"] = ""
    synthetic_kb(indicators=real_indicators)
    with pytest.raises(RuntimeError, match="skoda_gas.brake_fluid_low"):
        kb.indicators()


def test_request_against_kb_with_empty_manual_id_gets_error_reply(client, synthetic_kb, real_indicators):
    real_indicators[0]["provenance"]["manual_id"] = ""
    synthetic_kb(indicators=real_indicators)
    resp = client.post("/", json={"language": "en-US", "messages": [{"role": "user", "content": "brake fluid warning"}]})
    assert resp.status_code == 200
    assert resp.json()["reply"].startswith("An error occurred while processing the request.")


@pytest.mark.parametrize("blank", [" ", "\t \n"])
def test_record_with_blank_manual_id_is_rejected(synthetic_kb, real_indicators, blank):
    real_indicators[0]["provenance"]["manual_id"] = blank
    synthetic_kb(indicators=real_indicators)
    with pytest.raises(RuntimeError, match="skoda_gas.brake_fluid_low"):
        kb.indicators()
