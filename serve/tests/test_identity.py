"""
The vehicle of a request: the caller number and the plate number field, the precedence between them
and the customer id, the text that is never read for a plate, and the do-not-drive line without a car.
"""
import asyncio
import concurrent.futures
import itertools
import json
import random
import re
import statistics
import string
import time
import unicodedata
from contextlib import asynccontextmanager

import pytest

import app.luna as luna
from app import agent, desk, kb, main, orchestrator
from app.compose import compose_escalate, stop_precaution
from app.match import _TABLE
from app.orchestrator import (
    _build_snippet,
    _is_secret,
    stop_warning,
    stop_warning_or_fallback,
)
from app.providers import FakeProvider, ToolCall, Turn
from app.tools import (
    customer_by_phone,
    drivability_assessment,
    field_given,
    normalise_plate,
    phone_digits,
    sheet_cells,
    vehicle_for_request,
)

RED = "red brake warning light"
G, NS, VU, MM = "skoda_gas", "non_supported_vehicle", "vehicle_unknown", "vehicle_mismatch"
EN_STOP = "\n\n" + stop_precaution("en-US")
LF_EN = "check engine light came on again"


@pytest.fixture(autouse=True)
def _local_tools(monkeypatch):
    monkeypatch.delenv("CARMIND_MCP_URL", raising=False)
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")


def _v(text=RED, **kw):
    r = vehicle_for_request(text, **kw)
    return r["platform_cell"] or r["reason"]


def _chat(client, text, language="en-US", **fields):
    resp = client.post("/", json={"language": language, "messages": [{"role": "user", "content": text}], **fields})
    assert resp.status_code == 200
    return resp.json()["reply"]


def _agent(client, problem=RED, **fields):
    return client.post("/api/agent", json={"problem": problem, **fields})


def _raise(*a, **k):
    raise RuntimeError("unavailable")


def _fault(monkeypatch, name):
    """Make the fleet procedures ("fleet") or the indicator lookup ("lights") fail; "none" changes nothing.

    "customer" makes the customer lookup fail and "load" makes the record loader fail.
    """
    if name == "fleet":
        monkeypatch.setattr(kb, "procedure", _raise)
        monkeypatch.setattr(orchestrator, "get_fleet_procedure", _raise)
    elif name == "lights":
        monkeypatch.setattr(orchestrator, "lookup_indicator", _raise)
    elif name == "customer":
        monkeypatch.setattr(kb, "customer", _raise)
    elif name == "load":
        monkeypatch.setattr(kb, "_load", _raise)


# 1-2. The fields
@pytest.mark.parametrize("value", ["FH-104-27", "fh-104-27", "fh 104 27", "FH10427", " FH-104-27 ", "FH 104-27"])
def test_plate_field_is_normalised(value):
    assert normalise_plate(value) == "FH-104-27"


@pytest.mark.parametrize("value", ["", None, 5, "FH_104_27", "FH-104-278", "\uff26\uff28-104-27", "F-104-27",
                                   "FH-104-2١", " " * 30 + "FH-104-27" + " " * 5])
def test_anything_else_is_not_a_plate(value):
    assert normalise_plate(value) is None


@pytest.mark.parametrize("value", ["+1-555-0142", "1 (555) 0142", "1.555.0142"])
def test_caller_number_is_read_as_digits(value):
    assert phone_digits(value) == "15550142"


@pytest.mark.parametrize("value", ["", "abc", "+1-555-0142x", "++1555", 15550142, "1" * 41])
def test_anything_else_is_no_number(value):
    assert phone_digits(value) is None


PHONE_ROWS = [
    ("+1-555-0142", True), ("1 (555) 0142", True), ("1.555.0142", True), (" +1-555-0142 ", True),
    (" +1-555-0142", True), ("+1-555-0142x", False), ("++1-555-0142", False), ("1-555-0142+", False),
    ("+1/555/0142", False), ("\uff11-555-0142", False), ("+1-555-0142\ufeff", False),
]


@pytest.mark.parametrize("value,known", PHONE_ROWS)
def test_caller_number_reading(value, known):
    assert ((customer_by_phone(value) or {}).get("id") == "C-1001") == known


def test_field_given():
    assert [field_given(v) for v in (None, "", "  ", "x", 5)] == [False, False, False, True, True]


# 3. Precedence
@pytest.mark.parametrize("kw,expected", [
    ({"caller_number": "+1-555-0142"}, G),
    ({"caller_number": "1 555 0142"}, G),
    ({"caller_number": "+1-555-0177"}, "choose_vehicle"),
    ({"caller_number": "+1-555-0177", "plate": "fh 452 63"}, G),
    ({"caller_number": "+1-555-0177", "plate": "FH-587-14"}, NS),
    ({"caller_number": "+1-555-0177", "plate": "FH-104-27"}, "plate_not_customers"),
    ({"caller_number": "+1-555-0177", "plate": "FH-999-99"}, "plate_not_customers"),
    ({"caller_number": "+1-555-0199"}, "unknown_caller"),
    ({"caller_number": "+1-555-0199", "plate": "FH-221-90"}, G),
    ({}, VU),
    ({"caller_number": "abc"}, "unknown_caller"),
    ({"caller_number": "+1-555-0142", "text": "Toyota RAV4 red brake warning light"}, MM),
    ({"caller_number": "+1-555-0163"}, NS),
    ({"plate": "FH-104-27", "customer_id": "C-1002"}, "plate_not_customers"),
    ({"customer_id": "C-1001", "caller_number": "+1-555-0118"}, "customer_mismatch"),
    ({"customer_id": "C-1001", "caller_number": "+1-555-0199"}, "customer_mismatch"),
    ({"customer_id": "C-1001"}, G),
    ({"customer_id": "C-1004"}, "choose_vehicle"),
    ({"plate": "FH-999-99", "text": "Skoda Octavia petrol red brake warning light"}, "plate_unknown"),
    ({"text": "Skoda Octavia petrol red brake warning light"}, G),
    ({"text": "Skoda Octavia petrol red brake warning light", "text_route": False}, VU),
    ({"caller_number": "+1-555-0199", "text": "Skoda Octavia petrol red brake warning light"}, G),
    ({"caller_number": "+1-555-0199", "text": "Toyota RAV4 check engine light"}, NS),
    ({"customer_id": "C-1001", "caller_number": "+1-555-0142x"}, "customer_mismatch"),
    ({"customer_id": "C-1001", "caller_number": 5}, "customer_mismatch"),
    ({"customer_id": "C-9999"}, "unknown_customer"),
    ({"plate": "x" * 41}, "plate_unknown"),
    ({"caller_number": "1" * 41}, "unknown_caller"),
])
def test_precedence(kw, expected):
    text = kw.pop("text", RED)
    assert _v(text, **kw) == expected


def test_choose_vehicle_lists_only_the_callers_cars():
    r = vehicle_for_request(RED, caller_number="+1-555-0177")
    assert [v["plate"] for v in r["vehicles"]] == ["FH-452-63", "FH-587-14"]
    assert r["customer"]["id"] == "C-1004"


def test_null_and_empty_plate_are_the_same():
    for kw in ({}, {"customer_id": "C-1001"}):
        assert vehicle_for_request(RED, plate=None, **kw) == vehicle_for_request(RED, plate="", **kw)


# 4. Makes that are also ordinary words void the car as written or in capitals
@pytest.mark.parametrize("text", ["My MG4 has the ESC light flashing", "Seat belt light on"])
def test_weak_make_as_written_voids_the_car(client, text):
    assert _v(text, plate="FH-104-27") == MM
    assert _chat(client, text, plate="FH-104-27") == compose_escalate("en-US", MM)


def test_weak_make_with_a_stop_light_keeps_the_precaution(client):
    text = "Red brake warning light. Seat heater also off"
    assert _v(text, plate="FH-104-27") == MM
    assert _chat(client, text, plate="FH-104-27") == compose_escalate("en-US", MM) + EN_STOP


@pytest.mark.parametrize("text", ["my mg zs red brake warning light", "a seat heater, red brake warning light"])
def test_weak_make_in_lower_case_does_not_void_the_car(text):
    assert _v(text, plate="FH-104-27") == G


def test_text_route_weak_make_stays_unsupported():
    assert _v("MG ZS check engine light") == NS


# 5. The text is never read for a plate or a number
TEXT_ROWS = [
    ("FH-104-27 red brake warning light", G, VU),
    ("fh 104 27 red brake warning light", G, VU),
    ("fh10427 red brake warning light", G, VU),
    ("לFH-104-27 נורת נוזל הבלמים"
     " דלוקה", G, VU),
    ("FH-104-27\u200b red brake warning light", G, VU),
    ("my plate is FH-104-27\u200b8; red brake warning light", G, VU),
    ("my plate is FH-104-27\x0b8; red brake warning light", G, VU),
    ("8\u200d-FH-104-27 red brake warning light", G, VU),
    ("my plate is FH-104-27١; red brake warning light", G, VU),
    ("FH-104-27-8 red brake warning light", G, VU),
    ("plate:FH-104-27 red brake warning light", G, VU),
    ("red brake warning light, plate:AB-123-45", G, VU),
    ("red brake warning light, AB-123-45.Thanks", G, VU),
    ("red brake warning light, my other car AB 123 45 10 days ago", G, VU),
    ("FH-104-27,red brake warning light", G, VU),
    ("\u24b6FH-104-27 red brake warning light", G, VU),
    ("FH-318-05 red brake warning light", G, VU),
    ("FH-999-99 red brake warning light", G, VU),
    ("FH-104-27 FH-318-05 red brake warning light", G, VU),
    ("RED BRAKE WARNING LIGHT AT KM 120 000", G, VU),
    # Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
    ("RED BRAKE WARNING LIGHT, CALL ME 555 0123", G, VU),
    ("red brake warning light, TEL 555-0123", G, VU),
    ("RED BRAKE WARNING LIGHT AT 120 00KM", G, VU),
    ("red brake warning light, CALL ME AT 555-01-23", G, VU),
    ("red brake warning light, ORDER NO AB-123-45678", G, VU),
    ("AB-123-45-678 red brake warning light", G, VU),
    ("FH-104-27 Toyota RAV4 red brake warning light", MM, NS),
    ("FH-221-90 Skoda Kamiq red brake warning light", MM, VU),
    ("FH-104-27 my Superb has the red brake warning light on", MM, NS),
    ("Skoda Octavia petrol plate:AB-123-45 red brake warning light", G, G),
    ("FH-104-27 red brake warning light, a Jeep hit me", MM, NS),
    ("FH-104-27 Skoda Rapid red brake warning light", MM, NS),
]
_IDS = [f"T{i}" for i in range(1, len(TEXT_ROWS) + 1)]


@pytest.mark.parametrize("text,with_plate,empty", TEXT_ROWS, ids=_IDS)
def test_text_is_not_read_tools(text, with_plate, empty):
    assert _v(text, plate="FH-104-27") == with_plate
    assert _v(text) == empty


@pytest.mark.parametrize("text,with_plate,empty", TEXT_ROWS, ids=_IDS)
def test_text_is_not_read_contract(client, text, with_plate, empty):
    for fields, expected in (({"plate": "FH-104-27"}, with_plate), ({}, empty)):
        reply = _chat(client, text, **fields)
        if expected == G:
            assert "Do not drive." in reply and "Manufacturer source" in reply, reply
        else:
            assert reply == compose_escalate("en-US", expected) + EN_STOP


def test_text_is_not_read_contract_hebrew(client):
    text = TEXT_ROWS[3][0]
    reply = _chat(client, text, "he-IL", plate="FH-104-27")
    assert "אין לנסוע" in reply and "מדריך הצי" in reply
    he = _chat(client, text, "he-IL")
    assert he == compose_escalate("he-IL", VU) + "\n\n" + stop_precaution("he-IL")


@pytest.mark.parametrize("text,with_plate,empty", TEXT_ROWS, ids=_IDS)
def test_text_is_not_read_desk(text, with_plate, empty):
    for kw in ({"plate": "FH-104-27"}, {"customer_id": "C-1001"}):
        a = desk.assess_warning_light(kw.get("customer_id", ""), text, plate=kw.get("plate", ""))
        assert a["status"] == "not_drivable", (kw, a)
        if with_plate == MM:
            assert a["verdict_text"] == compose_escalate("en-US", MM) + EN_STOP
        else:
            assert "Brake fluid level too low" in a["verdict_text"]
    a = desk.assess_warning_light("", text)
    assert a["status"] == "not_drivable"
    assert a["verdict_text"] == compose_escalate("en-US", VU, text_route=False) + EN_STOP


@pytest.mark.parametrize("index", [0, 16, 21, 26])
def test_text_is_not_read_agent(client, index):
    text, with_plate, _empty = TEXT_ROWS[index]
    data = _agent(client, text, plate="FH-104-27").json()
    assert data["verdict"]["status"] == "not_drivable"
    if with_plate == MM:
        assert data["verdict"]["text"] == compose_escalate("en-US", MM) + EN_STOP
    else:
        assert "Brake fluid level too low" in data["verdict"]["text"]


# 6. Do not drive without a car
def test_sheet_cells_and_stop_records():
    assert sheet_cells() == ["skoda_gas"]
    stops = {rec["id"].split(".")[-1] for rec in kb.indicators().values()
             if drivability_assessment(rec)["status"] == "not_drivable"}
    assert stops == {"brake_fluid_low", "brake_abs_fault"}


def test_stop_light_without_a_car_gets_the_precaution(client):
    assert kb.indicator("skoda_gas.brake_fluid_low")["vehicle_scope"] == ["skoda_gas"]
    assert _chat(client, RED) == compose_escalate("en-US", VU) + EN_STOP


CORPUS = [
    "red brake warning light", "check engine light", "front assist collision warning active", "coolant level low",
    "ESC light flashing", "coolant light and check engine light both on",
    "ESC light stays on continuously, not flashing", "Kamiq ESC light",
    "abs warning light is flashing", "brake system warning light on", "brake fluid light and coolant light both on",
    "ESC and front assist lights both stay on", "coolant temperature too high, engine overheating",
    "the brakes failed, pedal goes to the floor",
    "נורת נוזל הבלמים דלוקה",
    "נורת ABS נדלקה",
    "נורת קירור דולקת"
    " והמנוע מתחמם",
]


@pytest.mark.parametrize("flags", [[], ["repeat_problem"], ["after_service_hours"]])
@pytest.mark.parametrize("text", CORPUS)
def test_precaution_matches_the_identified_reply(client, text, flags):
    identified = _chat(client, text, plate="FH-104-27", context_flags=flags)
    bare = _chat(client, text, context_flags=flags)
    assert ("do not drive" in identified.casefold()) == bare.endswith(EN_STOP) == stop_warning(text, flags), (
        identified, bare)


@pytest.mark.parametrize("text", ["check engine light", "esc stability control light is flashing",
                                  "coolant level low", "front assist collision warning active"])
def test_no_precaution_for_a_light_that_is_not_a_stop(client, text):
    reply = _chat(client, text)
    assert reply == compose_escalate("en-US", VU)
    assert "drive" not in reply
    cars = kb.customer("C-1004")["vehicles"]
    assert _chat(client, text, caller_number="+1-555-0177") == compose_escalate("en-US", "choose_vehicle", cars)


OUTCOMES = [
    ({"plate": "FH-104-27"}, RED), ({}, RED), ({"caller_number": "+1-555-0199"}, RED),
    ({"plate": "FH-999-99"}, RED), ({"caller_number": "+1-555-0177"}, RED),
    ({"caller_number": "+1-555-0177", "plate": "FH-104-27"}, RED),
    ({"plate": "FH-104-27"}, "Toyota RAV4 red brake warning light"), ({"caller_number": "+1-555-0163"}, RED),
]
HE_RED = "נורת נוזל הבלמים דלוקה"


@pytest.mark.parametrize("fields,text", OUTCOMES)
def test_every_outcome_says_do_not_drive(client, fields, text):
    assert "Do not drive" in _chat(client, text, **fields)
    he_text = text.replace(RED, HE_RED)
    assert "אין לנסוע" in _chat(client, he_text, "he-IL", **fields)


@pytest.mark.parametrize("fields,text", OUTCOMES + [({"customer_id": "C-1001", "caller_number": "+1-555-0118"}, RED)])
def test_every_agent_outcome_says_do_not_drive(client, fields, text):
    resp = _agent(client, text, **fields)
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"]["status"] == "not_drivable"
    assert "Do not drive" in data["verdict"]["text"] and "Do not drive" in data["reply"]


def test_error_replies_keep_the_precaution(client):
    he = compose_escalate("he-IL", "error") + "\n\n" + stop_precaution("he-IL")
    resp = client.post("/", json={"language": 5, "messages": [{"role": "user", "content": RED}]})
    assert resp.json()["reply"] == he
    resp = client.post("/", json={"language": "en-US", "context_flags": ["bogus"],
                                  "messages": [{"role": "user", "content": RED}]})
    assert resp.json()["reply"] == compose_escalate("en-US", "error") + EN_STOP
    resp = client.post("/", content=b"{not json", headers={"content-type": "application/json"})
    assert resp.json()["reply"] == compose_escalate("he-IL", "error")


@pytest.mark.parametrize("body,expected", [
    ({"customer_id": "C-9999", "problem": RED}, "unknown customer"),
    ({"customer_id": "x" * 41, "problem": RED}, "unknown customer"),
    ({"customer_id": "C-1001", "problem": RED + " " + "B" * 4000}, "invalid request"),
])
def test_agent_rejections_keep_the_precaution(client, body, expected):
    resp = client.post("/api/agent", json=body)
    assert resp.status_code == 400
    assert resp.json() == {"error": expected, "precaution": stop_precaution("en-US")}


def test_agent_rejection_without_a_stop_has_no_precaution(client):
    resp = client.post("/api/agent", json={"customer_id": "C-1001", "problem": "B" * 4001})
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


def test_failed_assessment_tool_keeps_the_precaution(monkeypatch):
    def broken(customer_id, description, after_hours=False, caller_number="", plate=""):
        return {"error": "tool_failed"}

    monkeypatch.setitem(desk.TOOLS, "assess_warning_light", broken)
    out = asyncio.run(agent.run_agent("C-1001", RED, False, None, agent.LocalTools(), "no provider"))
    assert out["verdict"]["status"] == "not_drivable"
    assert out["verdict"]["text"] == compose_escalate("en-US", "error") + EN_STOP


FLAG_SETS = [[], ["repeat_problem"], ["after_service_hours"]]


def test_stop_warning_needs_no_fleet_procedure(monkeypatch):
    baseline = {(t, i): stop_warning(t, f) for t in CORPUS for i, f in enumerate(FLAG_SETS)}
    assert baseline[(RED, 0)] is True and baseline[("ESC light flashing", 0)] is False
    _fault(monkeypatch, "fleet")
    assert {(t, i): stop_warning(t, f) for t in CORPUS for i, f in enumerate(FLAG_SETS)} == baseline


def test_error_reply_keeps_the_precaution_when_the_fleet_procedure_fails(monkeypatch):
    _fault(monkeypatch, "fleet")
    body = {"language": "en-US", "messages": [{"role": "user", "content": RED}]}
    assert main._error_reply(body) == compose_escalate("en-US", "error") + EN_STOP


@pytest.mark.parametrize("fault", ["fleet", "lights"])
def test_every_error_outcome_keeps_the_precaution(client, monkeypatch, fault):
    _fault(monkeypatch, fault)
    error = compose_escalate("en-US", "error")
    assert _chat(client, RED, plate="FH-104-27") == error + EN_STOP
    he = compose_escalate("he-IL", "error") + "\n\n" + stop_precaution("he-IL")
    assert _chat(client, HE_RED, "he-IL", plate="FH-104-27") == he
    assert "Do not drive" in _chat(client, RED)
    for body, expected in (({"customer_id": "C-9999", "problem": RED}, "unknown customer"),
                           ({"customer_id": "C-1001", "problem": RED + " " + "B" * 4000}, "invalid request")):
        resp = client.post("/api/agent", json=body)
        assert resp.status_code == 400
        assert resp.json() == {"error": expected, "precaution": stop_precaution("en-US")}
    resp = _agent(client, customer_id="C-1001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verdict"]["status"] == "not_drivable"
    assert "Do not drive" in data["verdict"]["text"] and "Do not drive" in data["reply"]

    async def failing(*a, **k):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(agent, "run_agent", failing)
    resp = _agent(client, customer_id="C-1001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] == "internal error" and data["verdict"]["status"] == "not_drivable"
    assert data["reply"] == error + EN_STOP


@pytest.mark.parametrize("fault", ["fleet", "lights"])
def test_no_precaution_for_a_light_that_is_not_a_stop_under_a_fault(client, monkeypatch, fault):
    _fault(monkeypatch, fault)
    resp = client.post("/api/agent", json={"customer_id": "C-9999", "problem": "ESC light is flashing"})
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


@pytest.mark.parametrize("fault", ["customer", "load"])
def test_agent_route_keeps_the_precaution_when_the_customer_lookup_fails(client, monkeypatch, fault):
    _fault(monkeypatch, fault)
    resp = _agent(client, customer_id="C-1001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] == "internal error"
    assert data["verdict"]["status"] == "not_drivable"
    assert data["reply"] == compose_escalate("en-US", "error") + EN_STOP
    assert data["customer"] is None
    assert "Do not drive" in _chat(client, RED)


@pytest.mark.parametrize("fault", ["customer", "load"])
def test_agent_route_never_fails_under_a_customer_lookup_fault(client, monkeypatch, fault):
    _fault(monkeypatch, fault)
    for cid in ["C-1001", "C-9999", "c-1001", " C-1001 ", "x" * 40]:
        for problem in [RED, HE_RED, RED + " " + "B" * 3900]:
            resp = _agent(client, problem, customer_id=cid, caller_number="+1-555-0142", plate="FH-104-27")
            assert resp.status_code == 200
            data = resp.json()
            assert data["error"] == "internal error" and "Do not drive" in data["reply"]
    resp = _agent(client, customer_id="x" * 41)
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown customer", "precaution": stop_precaution("en-US")}


def test_fallback_never_drops_a_stop(monkeypatch):
    baseline = {(t, i): stop_warning(t, f) for t in CORPUS for i, f in enumerate(FLAG_SETS)}
    _fault(monkeypatch, "lights")
    for t in CORPUS:
        for i, f in enumerate(FLAG_SETS):
            if baseline[(t, i)]:
                assert stop_warning_or_fallback(t, f) is True, (t, f)
    assert stop_warning_or_fallback("", []) is False
    monkeypatch.setattr(orchestrator, "match", _raise)
    assert stop_warning_or_fallback("ESC light is flashing", []) is True


@pytest.mark.parametrize("fault", ["none", "fleet", "lights"])
def test_error_reply_fuzz(monkeypatch, fault):
    _fault(monkeypatch, fault)
    rng = random.Random(7)
    contents = [RED, HE_RED, 5, None, "", "ESC light is flashing"]
    flag_values = [None, "x", ["bogus"], ["after_service_hours"], [5]]

    def messages(content):
        return rng.choice([None, "x", [], [{"role": "user", "content": content}],
                           [{"role": "user", "content": content}, 5, "x", None,
                            {"role": "assistant", "content": RED}]])

    bodies = []
    for _ in range(1500):
        shape = rng.choice(["none", "list", "str", "dict"])
        if shape == "dict":
            bodies.append({"language": rng.choice([None, 5, "", "en-US", "he-IL", "ar-SA", "ru-RU"]),
                           "messages": messages(rng.choice(contents)), "context_flags": rng.choice(flag_values)})
        else:
            bodies.append({"none": None, "list": [], "str": "x"}[shape])

    def expected(body):
        body = body if isinstance(body, dict) else {}
        lang = body.get("language")
        lang = lang if isinstance(lang, str) and lang else "he-IL"
        msgs = body.get("messages") if isinstance(body.get("messages"), list) else []
        users = [m["content"] for m in msgs if isinstance(m, dict) and m.get("role") == "user"
                 and isinstance(m.get("content"), str)]
        return lang, bool(users) and users[-1] in (RED, HE_RED)

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        replies = list(pool.map(main._error_reply, bodies))
    for body, reply in zip(bodies, replies):
        lang, stop = expected(body)
        assert reply.startswith(compose_escalate(lang, "error")), (body, reply)
        assert reply.endswith("\n\n" + stop_precaution(lang)) == stop, (body, reply)


class McpLike:
    """Tools reached the way the MCP client reaches them: a failing tool comes back as an error result."""

    def __init__(self):
        self.calls = []

    @asynccontextmanager
    async def connect(self):
        async def call(name, args):
            self.calls.append(name)
            if name == "assess_warning_light":
                return {"error": "Error executing tool assess_warning_light"}
            return await agent.LocalTools.call(name, args)

        yield agent.ToolBox([], call)


def _assert_mcp_failure_keeps_the_precaution():
    tools = McpLike()
    out = asyncio.run(agent.run_agent("C-1001", RED, False, None, tools, "no provider"))
    assert out["verdict"]["text"] == compose_escalate("en-US", "error") + EN_STOP
    assert out["verdict"]["status"] == "not_drivable"
    assert "assess_warning_light" in tools.calls


def test_failed_assessment_tool_keeps_the_precaution_over_mcp(monkeypatch):
    def broken(customer_id, description, after_hours=False, caller_number="", plate=""):
        return {"error": "tool_failed"}

    monkeypatch.setitem(desk.TOOLS, "assess_warning_light", broken)
    _assert_mcp_failure_keeps_the_precaution()


def test_failed_assessment_over_mcp_keeps_the_precaution_when_the_lights_fail(monkeypatch):
    _fault(monkeypatch, "lights")
    _assert_mcp_failure_keeps_the_precaution()


def test_failed_assessment_after_inference_keeps_context_and_the_precaution(monkeypatch):
    monkeypatch.setenv("LUNA_MODE", "on")

    def broken(customer_id, description, after_hours=False, caller_number="", plate=""):
        return {"error": "tool_failed"}

    monkeypatch.setitem(desk.TOOLS, "assess_warning_light", broken)
    monkeypatch.setattr(desk, "assess_for_agent", _raise)
    out = asyncio.run(agent.run_agent("C-1002", LF_EN, False, FakeProvider(), agent.LocalTools()))
    assert out["context"]["flags"] == ["repeat_problem"]
    assert out["verdict"]["status"] == "not_drivable"
    assert out["verdict"]["text"] == compose_escalate("en-US", "error") + EN_STOP


def test_post_error_after_inference_keeps_the_precaution(client, monkeypatch):
    monkeypatch.setenv("LUNA_MODE", "on")
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")
    _fault(monkeypatch, "fleet")
    assert _chat(client, LF_EN, plate="FH-104-27") == compose_escalate("en-US", "error") + EN_STOP


# 7. The model never sees the phone number, and the first prompt carries no caller number or plate
def test_the_model_does_not_see_the_phone_number():
    class Recording:
        name = "recording"

        def __init__(self):
            self.seen = []
            self.turns = [Turn("", [ToolCall("a", "get_customer", {})])]

        async def complete(self, system, messages, tools):
            self.seen.append([dict(m) for m in messages])
            return self.turns.pop(0) if self.turns else Turn("")

    provider = Recording()
    out = asyncio.run(agent.run_agent(None, "check engine light", False, provider, agent.LocalTools(),
                                      caller_number="+1-555-0142", plate="FH-104-27"))
    assert out["customer"]["phone"] == "+1-555-0142"
    contents = [str(m.get("content", "")) for seen in provider.seen for m in seen]
    assert any("John Doe" in c for c in contents)
    assert not any("555-01" in c for c in contents)
    first = provider.seen[0][0]["content"]
    assert "FH-104-27" not in first and "555" not in first


# 8. Plate-shaped tokens never reach the snippet
def test_plate_shapes_are_masked_from_the_snippet(client, monkeypatch):
    seen = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None: seen.append(context) or None)
    reply = _chat(client, "my plate is FH-104-27, coolant light and check engine light both on", plate="FH-104-27")
    assert reply.startswith("Multiple possible warning lights")
    assert len(seen) == 1 and "FH" not in seen[0], seen


@pytest.mark.parametrize("plate", ["FH.104.27", "FH_104_27", "FH\u2013104\u201327", "FH\u2010104\u201027",
                                   "FH\u05be104\u05be27", "FH\uff0d104\uff0d27", "FH.104-27", "FH 104_27"])
def test_plate_shapes_with_any_separator_are_masked(plate):
    assert _build_snippet(f"my car {plate} coolant light", ["coolant"]) == "my car [REDACTED] coolant light"


def test_a_plate_with_a_space_is_masked_whole():
    assert "FH" not in _build_snippet("my car FH 104_27 coolant light", ["coolant"])


def test_every_dash_separates_a_plate_shape():
    dashes = [chr(i) for i in range(0x110000) if unicodedata.category(chr(i)) == "Pd"]
    assert "-" in dashes
    for d in dashes:
        assert _build_snippet(f"FH{d}104{d}27 coolant light", ["coolant"]) == "[REDACTED] coolant light", ascii(d)


def test_snippet_never_keeps_part_of_a_plate():
    for pad in range(60):
        snippet = _build_snippet("FH-104-27 " + "x" * pad + " coolant", ["coolant"])
        assert not any(ch.isdigit() for ch in snippet), (pad, snippet)


SNIPPET_PHRASES = ["brake fluid light", "ABS light", "coolant light", "check engine light", "front assist light",
                   "ESC light flashing"]
SNIPPET_KW = ["check engine", "engine light", "coolant"]
SNIPPET_LEAKS = ("FH", "qz", "zx", "xv", "vw", "ample", ".com", "someone", "exam")


def _light_keywords(ids):
    return [kw for bare in ids for row in _TABLE if row[0] == bare for kw in (*row[1], *row[2])]


def _snippet_cases():
    cases = []
    for n in (1, 2, 3):
        for combo in itertools.combinations(SNIPPET_PHRASES, n):
            text = " and ".join(combo) + " call me on 555 0123 or someone@example.com, plate FH-104-27"
            result = orchestrator.classify(text, "skoda_gas", [])
            if result["kind"] == "ambiguous" and result["luna_ids"]:
                cases.append((text, _light_keywords(result["luna_ids"])))
    assert len(cases) == 10
    for k in range(80):
        for tail in (" qzxvw@qzxvw.example.com and the coolant light", " 555.0142 and the coolant light",
                     " (555) 014-2 and the coolant light"):
            cases.append(("check engine light " + "y" * k + tail, SNIPPET_KW))
    cases.append(("Call 555.0142; check engine light and coolant light", SNIPPET_KW))
    for x in ("qzxvw\uff20qzxvw.example.com", "qzxvw\ufe6bqzxvw.example.com",
              "\u2075\u2075\u2075\u2070\u00b9\u2074\u00b2", "\u2460\u2461\u2462"):
        cases.append(("coolant light and check engine light, reach me at " + x, SNIPPET_KW))
    return cases


def _edge_cases():
    right = ["coolant light and check engine light qzxvw" + "x" * 600 + tail
             for tail in ("@example.com", "5", "\uff20example.com")]
    left = [head + "x" * 600 + "qzxvw coolant light and check engine light"
            for head in ("qzxvw@", "5", "qzxvw\uff20")]
    return right + left


def _assert_no_leak(snippet, text):
    for form in (snippet, unicodedata.normalize("NFKC", snippet)):
        assert not any(c.isdigit() for c in form), (text, snippet)
        assert "@" not in form, (text, snippet)
        for piece in SNIPPET_LEAKS:
            assert piece not in form, (piece, text, snippet)


TOKEN_REPRODUCTIONS = ("check engine light qzxvw12345 and coolant light",
                       "coolant light privateMailbox!tag@example.com and check engine light")
TOKEN_FRAGMENTS = ("qzx", "qzxvw", "privateMailbox", "tag")


def _assert_no_token_fragment(text):
    for snippet in (_build_snippet(text, SNIPPET_KW), _build_snippet(text, SNIPPET_KW, 120, 400)):
        for piece in TOKEN_FRAGMENTS:
            assert piece not in snippet, (piece, text, snippet)


def _assert_edge_clean(snippet, text):
    assert "qz" not in snippet and "x" * 5 not in snippet, (text, snippet)
    assert not any(c.isdigit() for c in snippet) and "@" not in unicodedata.normalize("NFKC", snippet), (text, snippet)
    assert "coolant" in snippet, (text, snippet)


def test_snippet_scrubs_every_token_with_a_digit_or_at():
    for text, keywords in _snippet_cases():
        _assert_no_leak(_build_snippet(text, keywords), text)
    for text in TOKEN_REPRODUCTIONS:
        _assert_no_token_fragment(text)
    for text in _edge_cases():
        _assert_edge_clean(_build_snippet(text, SNIPPET_KW), text)
        _assert_edge_clean(_build_snippet(text, SNIPPET_KW, 120, 400), text)


def test_flags_context_is_scrubbed():
    for text, keywords in _snippet_cases():
        snippet = _build_snippet(text, keywords, 120, 400)
        assert len(snippet) <= 400, (text, snippet)
        _assert_no_leak(snippet, text)
    for text in TOKEN_REPRODUCTIONS:
        _assert_no_token_fragment(text)
    for text in _edge_cases():
        _assert_edge_clean(_build_snippet(text, SNIPPET_KW), text)
        _assert_edge_clean(_build_snippet(text, SNIPPET_KW, 120, 400), text)


def test_snippet_keyword_offsets_hold_when_lowercasing_changes_length():
    assert "coolant" in _build_snippet("\u0130" * 60 + "coolant light and check engine light", SNIPPET_KW)


def test_overlapping_snippet_windows_are_merged():
    snippet = _build_snippet("coolant light and check engine light.", SNIPPET_KW)
    assert snippet.count("coolant") == 1, snippet


# 9. Desk edge cases
def test_desk_edge_cases():
    assert desk.get_customer("C-9999", plate="FH-104-27")["error"] == "unknown_customer"
    assert "error" in desk.plan_dispatch("C-9999", RED)
    assert "error" in desk.plan_dispatch("", RED)
    d = desk.plan_dispatch("", RED, caller_number="+1-555-0177")
    assert (d["status"], d["action"]) == ("not_drivable", "tow_to_dealer")
    d = desk.plan_dispatch("", "check engine light", caller_number="+1-555-0177")
    assert (d["status"], d["action"]) == ("unclear", "roadside_rescue")
    d = desk.plan_dispatch("C-1003", RED)
    assert (d["status"], d["action"], d["covered"]) == ("not_drivable", "tow_to_dealer", True)
    assert desk.assess_warning_light("C-1003", RED)["verdict_text"].startswith("No information found")


# 10. Data
def test_customer_data_shape(client):
    customers = kb.customers()
    assert len(customers) == 4
    phones = [c["phone"] for c in customers]
    assert all(re.fullmatch(r"\+1-555-01[0-9]{2}", p) for p in phones) and len(set(phones)) == 4
    plates = [v["plate"] for c in customers for v in c["vehicles"]]
    assert all(re.fullmatch(r"[A-Z]{2}-[0-9]{3}-[0-9]{2}", p) for p in plates) and len(set(plates)) == len(plates)
    assert all(set(v) == {"plate", "make", "model", "powertrain", "year"} for c in customers for v in c["vehicles"])
    counts = {len(c["vehicles"]) for c in customers}
    assert 1 in counts and max(counts) >= 2
    raw = (kb._resolve_root() / "knowledge" / "customers.json").read_bytes()
    assert raw.isascii() and b"@" not in raw
    assert "@" not in json.dumps(client.get("/api/customers").json())


# 11. Cost stays linear, and no field value raises
def _median_time(fn, arg):
    times = []
    for _ in range(3):
        started = time.perf_counter()
        fn(arg)
        times.append(time.perf_counter() - started)
    return statistics.median(times)


def test_long_text_with_a_plate_field_is_linear():
    def run(n):
        return vehicle_for_request("red brake warning light " + "KM 120 000 " * n, plate="FH-104-27")
    assert _median_time(run, 50_000) / _median_time(run, 25_000) < 3.0


def test_stop_warning_is_linear():
    def run(n):
        return stop_warning("FH-104-27 " * n, [])
    assert _median_time(run, 50_000) / _median_time(run, 25_000) < 3.0


def test_snippet_is_linear():
    def run(n):
        return _build_snippet("FH.104.2 " * n + "coolant", ["coolant"])
    assert _median_time(run, 50_000) / _median_time(run, 25_000) < 3.0


@pytest.mark.parametrize("window,limit", [(40, 200), (120, 400)])
def test_snippet_is_linear_on_combining_marks(window, limit):
    def run(n):
        return _build_snippet("a" + "\u0315" * n + "\u0300" * n + " coolant", ["coolant"], window, limit)
    assert _median_time(run, 8_000) / _median_time(run, 4_000) < 3.0


def test_is_secret_matches_whole_token_nfkc():
    alphabet = list(string.ascii_letters + string.digits) + [
        "@", "\uff20", "\u0661", "\u2460", "\U0001d7ce", "\u00b2", "\u0300", "\u0315", "\u0301", "\u0130", "\u212a",
        "\uff26", "\u1100", "\u1161", "\u200b"]
    rng = random.Random(3)
    for _ in range(20_000):
        t = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 10)))
        assert _is_secret(t) == bool(re.search(r"[\d@]", unicodedata.normalize("NFKC", t))), repr(t)


def test_random_field_values_never_raise():
    rng = random.Random(1)
    pool = [None, "", " ", 5, 1.5, True, [], {}, "x" * 41, "+1-555-0142", "+1-555-0177", "+1-555-0199", "FH-104-27",
            "fh 452 63", "FH-999-99", "C-1001", "C-1004", "C-9999", "١", "\uff26\uff28-104-27", "--", "()"]
    for _ in range(2000):
        kw = {k: rng.choice(pool) for k in ("caller_number", "plate", "customer_id")}
        r = vehicle_for_request(rng.choice(CORPUS), text_route=rng.choice((True, False)), **kw)
        assert set(r) == {"platform_cell", "reason", "customer", "vehicle", "vehicles"}


# 12. The error replies when the thread pool cannot start a thread
async def _no_thread(*a, **k):
    raise RuntimeError("can't start new thread")


async def _failing_agent(*a, **k):
    raise RuntimeError("unavailable")


@pytest.fixture
def _no_threads(monkeypatch):
    monkeypatch.setattr(main, "run_in_threadpool", _no_thread)


@pytest.mark.usefixtures("_no_threads")
@pytest.mark.parametrize("lang", ["en-US", "he-IL"])
def test_contract_error_reply_without_threads(client, lang):
    reply = _chat(client, RED, lang)
    assert reply == compose_escalate(lang, "error") + "\n\n" + stop_precaution(lang)
    if lang == "en-US":
        assert "Do not drive" in reply


@pytest.mark.usefixtures("_no_threads")
def test_contract_no_stop_without_threads(client):
    assert _chat(client, "ESC light is flashing") == compose_escalate("en-US", "error")


@pytest.mark.usefixtures("_no_threads")
def test_agent_unknown_customer_without_threads(client):
    resp = _agent(client, customer_id="C-9999")
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown customer", "precaution": stop_precaution("en-US")}


@pytest.mark.usefixtures("_no_threads")
def test_agent_overlength_without_threads(client):
    resp = _agent(client, RED + " " + "B" * 4000, customer_id="C-1001")
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid request", "precaution": stop_precaution("en-US")}


@pytest.mark.usefixtures("_no_threads")
def test_agent_no_stop_rejection_without_threads(client):
    resp = _agent(client, "B" * 4001, customer_id="C-1001")
    assert resp.status_code == 400 and set(resp.json()) == {"error"}


@pytest.mark.usefixtures("_no_threads")
def test_agent_guarded_without_threads(client, monkeypatch):
    monkeypatch.setattr(agent, "run_agent", _failing_agent)
    resp = _agent(client, customer_id="C-1001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] == "internal error" and data["verdict"]["status"] == "not_drivable"
    assert "Do not drive" in data["reply"]


@pytest.mark.usefixtures("_no_threads")
def test_agent_happy_path_without_threads(client):
    resp = _agent(client, customer_id="C-1001")
    assert resp.status_code == 200 and "Do not drive" in resp.json()["reply"]


@pytest.mark.usefixtures("_no_threads")
def test_last_resort_when_the_inline_check_fails(client, monkeypatch):
    monkeypatch.setattr(main, "stop_warning_or_fallback", _raise)
    assert _chat(client, "ESC light is flashing") == compose_escalate("en-US", "error") + EN_STOP
    assert _chat(client, "") == compose_escalate("en-US", "error")
    resp = _agent(client, "ESC light is flashing", customer_id="C-9999")
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown customer", "precaution": stop_precaution("en-US")}
    monkeypatch.setattr(agent, "run_agent", _failing_agent)
    resp = _agent(client, "ESC light is flashing", customer_id="C-1001")
    assert resp.status_code == 200 and resp.json()["verdict"]["status"] == "not_drivable"


@pytest.mark.usefixtures("_no_threads")
def test_error_paths_without_threads_under_volume(client):
    rng = random.Random(9)
    for _ in range(200):
        base = rng.choice([RED, "ESC light is flashing"])
        problem = (base + " ").ljust(rng.choice([1, 3999, 4000, 4001, 4002]), "B")
        if rng.choice(["/", "/api/agent"]) == "/":
            resp = client.post("/", json={"language": "en-US", "messages": [{"role": "user", "content": problem}]})
        else:
            resp = _agent(client, problem, customer_id=rng.choice(["C-1001", "C-9999"]))
        assert resp.status_code in {200, 400}
        data = resp.json()
        said = " ".join(str(v) for v in (data.get("reply"), (data.get("verdict") or {}).get("text"),
                                         data.get("precaution")))
        assert ("Do not drive" in said) == (base == RED), (base, len(problem), data)
