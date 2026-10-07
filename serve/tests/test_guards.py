"""
Safety-gate tests that go through the HTTP route: the LLM selector can never pick
when a stop-level light is a candidate, can only pick among matched candidates,
is never called with LUNA_MODE off, and errors fall back to a 200 escalate reply.
"""
import pytest

import app.luna as luna
from app import kb


def _post(client, text, language="en-US", plate="FH-104-27"):
    resp = client.post("/", json={
        "language": language,
        "messages": [{"role": "user", "content": text}],
        "plate": plate,
    })
    assert resp.status_code == 200
    return resp.json()["reply"]


def test_llm_not_consulted_with_stop_level_candidate(client, monkeypatch):
    calls = []

    def fake_select(question, context, options, deadline=None):
        calls.append([o.id for o in options])
        return luna.Outcome("picked", "coolant_level_low", "r")

    monkeypatch.setattr(luna, "select", fake_select)
    reply = _post(client, "brake fluid light and coolant light both on")
    assert calls == [], f"LLM consulted with a stop-level candidate present: {calls}"
    assert reply.startswith("Multiple possible warning lights"), f"not a clarifying reply: {reply!r}"
    assert "Brake fluid level too low" in reply
    assert "do not drive" in reply.lower(), f"missing precaution: {reply!r}"
    assert "Manufacturer source" not in reply, f"single verdict given: {reply!r}"


def test_llm_choice_outside_candidates_ignored(client, monkeypatch):
    monkeypatch.setattr(luna, "select",
                        lambda question, context, options, deadline=None: luna.Outcome("picked", "esc_active", "r"))
    reply = _post(client, "coolant light and check engine light both on")
    assert reply.startswith("Multiple possible warning lights"), f"not a clarifying reply: {reply!r}"
    assert "Manufacturer source" not in reply
    assert "Light picked" not in reply and "Model reasoning" not in reply, reply


NAMES = ["Coolant level too low", "Petrol engine control faulty"]


def test_llm_choice_only_selects_verdict_comes_from_record(client, monkeypatch):
    from app.compose import contract_choice_line
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        luna.Outcome("picked", "coolant_level_low", "r"))
    reply = _post(client, "coolant light and check engine light both on")
    assert "Severity: urgent" in reply, f"verdict not taken from the record: {reply!r}"
    assert "Conditional - check first before driving." in reply.splitlines(), reply
    assert "p.142" in reply
    assert reply.split("\n\n")[-1] == contract_choice_line("picked", "Coolant level too low", NAMES, ("coolant",),
                                                         "en-US")


def test_a_less_cautious_pick_is_accepted_and_names_every_light(client, monkeypatch):
    from app.compose import contract_choice_line
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        luna.Outcome("picked", "engine_control_petrol", "r"))
    reply = _post(client, "coolant light and check engine light both on")
    assert "Drive with caution." in reply.splitlines(), reply
    assert reply.split("\n\n")[-1] == contract_choice_line(
        "picked", "Petrol engine control faulty", NAMES, ("check engine", "engine light"), "en-US")


@pytest.mark.parametrize("mode", ["off", None])
def test_luna_off_makes_no_llm_call(client, monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("LUNA_MODE", raising=False)
    else:
        monkeypatch.setenv("LUNA_MODE", mode)
    from app import providers
    calls = []

    async def choose(self, request):
        calls.append("choose")

    async def choose_many(self, request):
        calls.append("choose_many")

    monkeypatch.setattr(providers, "get_provider", lambda *a, **k: calls.append("get_provider"))
    monkeypatch.setattr(providers.FakeProvider, "choose", choose)
    monkeypatch.setattr(providers.FakeProvider, "choose_many", choose_many)
    # Ambiguous with no stop-level candidate: the only path that reaches the LLM.
    reply = _post(client, "coolant light and check engine light both on")
    assert calls == [], f"LLM machinery touched with LUNA_MODE={mode!r}: {calls}"
    assert reply.startswith("Multiple possible warning lights")


def _patch_record(monkeypatch, qualified_id, **fields):
    from app import kb
    rec = dict(kb.indicator(qualified_id))
    rec.update(fields)
    monkeypatch.setitem(kb.indicators(), qualified_id, rec)


# Real records carry severity=stop_now and hard_stop=true together; each signal
# must be enough on its own.
_STOP_SIGNALS = [("stop_now", False), ("urgent", True)]


@pytest.mark.parametrize("severity,hard_stop", _STOP_SIGNALS)
def test_either_stop_signal_alone_forces_do_not_drive(client, monkeypatch, severity, hard_stop):
    _patch_record(monkeypatch, "skoda_gas.brake_fluid_low", severity=severity, hard_stop=hard_stop)
    reply = _post(client, "brake fluid warning")
    assert "Do not drive." in reply, f"no do-not-drive line: {reply!r}"
    assert "Stop driving! Seek the help of a specialist garage." in reply


@pytest.mark.parametrize("severity,hard_stop", _STOP_SIGNALS)
def test_either_stop_signal_alone_keeps_llm_out(client, monkeypatch, severity, hard_stop):
    _patch_record(monkeypatch, "skoda_gas.brake_fluid_low", severity=severity, hard_stop=hard_stop)
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or luna.Outcome("picked", "coolant_level_low", "r"))
    reply = _post(client, "brake fluid light and coolant light both on")
    assert calls == [], f"LLM consulted with a stop-level candidate present: {calls}"
    assert "do not drive" in reply.lower(), f"missing precaution: {reply!r}"


def test_malformed_json_returns_200_escalate(client):
    resp = client.post("/", content=b"{not json", headers={"content-type": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["reply"].startswith("אירעה שגיאה בעיבוד הבקשה")


def test_invalid_shape_returns_200_escalate_in_request_language(client):
    resp = client.post("/", json={"language": "en-US", "messages": "brake fluid"})
    assert resp.status_code == 200
    assert resp.json()["reply"].startswith("An error occurred while processing the request.")


def test_llm_snippet_is_minimized(client, monkeypatch):
    seen = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None: seen.append(context) or None)
    # Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
    text = ("x" * 300 + " call me on: 5550123 about the coolant light and check engine light both on "
            + "y" * 300)
    reply = _post(client, text)
    assert reply.startswith("Multiple possible warning lights")
    assert len(seen) == 1, f"LLM not consulted exactly once: {seen}"
    snippet = seen[0]
    assert len(snippet) <= 200, f"snippet too long ({len(snippet)})"
    assert "5550123" not in snippet and not any(c.isdigit() for c in snippet) and "[REDACTED]" in snippet, snippet
    assert "coolant" in snippet and "check engine" in snippet, snippet
    # Each keyword window reaches at most 40 chars into the surrounding text.
    assert "x" * 41 not in snippet and "y" * 41 not in snippet, snippet


def test_unsupported_vehicle_gets_vehicle_escalate(client):
    reply = _post(client, "my Tesla has a brake fluid warning", plate=None)
    assert reply.startswith("No information found for the specified vehicle."), reply


# Hebrew spellings of every non-Skoda make the service knows. Written out here rather
# than read from tools.py so that a lost alias is caught.
_HE_NON_SKODA = [
    "קיה", "פיג'ו", "יונדאי", "טויוטה", "טסלה", "צ'רי", "סיטרואן", "סובארו", "סיאט", "רנו",
    "דאצ'יה", "מאזדה", "הונדה", "אופל", "מיצובישי", "ניסאן", "פולקסווגן", "פיאט", "ג'נסיס",
    "ג'אקו", "זיקר", "ג'ילי", "צ'אנגן", "איסוזו", "שברולט",
    "אקספנג", "ב.מ.וו", "מרצדס", "אאודי", "וולוו", "פורד", "לקסוס", "סוזוקי",
    "במוו", "ביואיידי", "בי.וו.די", "בי.ווי.די", "ג'יקו",
]


@pytest.mark.parametrize("alias", _HE_NON_SKODA)
def test_hebrew_non_skoda_make_escalates(client, alias):
    reply = _post(client, f"יש לי {alias} ונדלקה נורת נוזל בלמים", language="he-IL", plate=None)
    assert reply.startswith("לא נמצא מידע עבור כלי הרכב"), f"{alias}: {reply!r}"


def test_unknown_light_escalates_in_hebrew(client):
    reply = _post(client, "נורה מוזרה דלוקה", language="he-IL")
    assert reply.startswith("לא הצלחנו לזהות את הנורה"), reply


@pytest.mark.parametrize("abs_token", ["ABS", "anti-lock", "anti lock"])
def test_brake_and_abs_resolve_to_abs_fault(client, abs_token):
    reply = _post(client, f"brake warning light and {abs_token} light are on")
    assert "Brake system and anti-lock braking system faulty" in reply, reply
    assert "p.128" in reply and "Do not drive." in reply


@pytest.mark.parametrize("text,verdict_line", [
    ("brake fluid warning", "Do not drive."),
    ("coolant level low", "Conditional - check first before driving."),
    ("check engine light", "Drive with caution."),
    ("esc stability control light is flashing", "Vehicle operational."),
])
def test_verdict_line_per_severity(client, text, verdict_line):
    reply = _post(client, text)
    assert verdict_line in reply.splitlines(), reply


def test_conditional_reply_carries_manual_condition(client):
    rec = kb.indicator("skoda_gas.coolant_level_low")
    reply = _post(client, "coolant level low")
    assert rec["message_text"] in reply, reply


def test_reply_shows_rulebook_entry_then_record(client):
    proc = kb.procedure("brake-fluid")
    rec = kb.indicator("skoda_gas.brake_fluid_low")
    reply = _post(client, "brake fluid warning")
    lines = reply.splitlines()
    assert lines[0] == "Fleet recovery guide:"
    assert lines[1] == f"{proc['light_name_he']} - {proc['explanation_he']}"
    assert lines[2] == proc["recovery_action_he"]
    assert f"{rec['name_en']}: {rec['meaning_en']}" in lines


def test_record_without_severity_escalates_through_route(client, synthetic_kb, real_indicators):
    # The schema allows an indicator with no severity; such a record must reach the
    # route and get the escalate verdict, not a drivable one.
    rec = next(r for r in real_indicators if r["id"] == "skoda_gas.engine_control_petrol")
    del rec["severity"]
    synthetic_kb(indicators=real_indicators)
    lines = _post(client, "check engine light").splitlines()
    assert "Contact a qualified specialist." in lines, lines
    for drivable in ("Drive with caution.", "Vehicle operational.",
                     "Conditional - check first before driving."):
        assert drivable not in lines, lines


def _drop_field(monkeypatch, qualified_id, field):
    rec = dict(kb.indicator(qualified_id))
    rec.pop(field)
    monkeypatch.setitem(kb.indicators(), qualified_id, rec)


def test_record_without_hard_stop_is_judged_by_severity(client, monkeypatch):
    _drop_field(monkeypatch, "skoda_gas.engine_control_petrol", "hard_stop")
    assert "Drive with caution." in _post(client, "check engine light").splitlines()


def test_ambiguous_records_without_hard_stop_are_not_stop_level(client, monkeypatch):
    _drop_field(monkeypatch, "skoda_gas.coolant_level_low", "hard_stop")
    _drop_field(monkeypatch, "skoda_gas.engine_control_petrol", "hard_stop")
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or None)
    reply = _post(client, "coolant light and check engine light both on")
    assert len(calls) == 1, calls
    assert "do not drive" not in reply.lower(), reply


def test_snippet_window_is_40_chars_each_side():
    from app.orchestrator import _build_snippet
    text = "a" * 50 + "coolant" + "b" * 50
    assert _build_snippet(text, ["coolant"]) == "a" * 40 + "coolant" + "b" * 40


def test_snippet_keyword_at_start_of_text():
    from app.orchestrator import _build_snippet
    text = "coolant" + "b" * 50
    assert _build_snippet(text, ["coolant"]) == "coolant" + "b" * 40


def test_invalid_shape_without_language_gets_hebrew_error(client):
    resp = client.post("/", json={"messages": "brake fluid"})
    assert resp.status_code == 200
    assert resp.json()["reply"].startswith("אירעה שגיאה בעיבוד הבקשה"), resp.json()


def test_hebrew_ambiguous_stop_level_lists_lights_and_warns(client):
    reply = _post(client, "נוזל בלמים ונוזל קירור", language="he-IL")
    assert reply.startswith("זוהו מספר נורות אפשריות:"), reply
    assert "אזהרה: ייתכן ומדובר בתקלה חמורה - אין לנסוע עד לבירור." in reply, reply


def test_english_no_match_reply(client):
    reply = _post(client, "please describe the unknown warning light")
    assert reply.startswith("We could not identify the dashboard light"), reply


@pytest.mark.parametrize("text,record_line", [
    ("ESC light stays on continuously, not flashing", "Vehicle operational."),
    ("coolant temperature too high, engine overheating", "Conditional - check first before driving."),
])
def test_state_the_record_excludes_gets_no_record_verdict(client, text, record_line):
    reply = _post(client, text)
    assert record_line not in reply.splitlines(), reply
    assert "Manufacturer source" not in reply, reply
    assert reply.startswith("We could not identify the dashboard light"), reply


@pytest.mark.parametrize("text,language,prefix", [
    ("Skoda Enyaq ESC light", "en-US", "No information found for the specified vehicle."),
    ("Skoda Octavia diesel check engine light", "en-US", "No information found for the specified vehicle."),
    ("סקודה סופרב נורת מנוע", "he-IL", "לא נמצא מידע עבור כלי הרכב"),
])
def test_non_petrol_skoda_is_rejected(client, text, language, prefix):
    assert _post(client, text, language=language, plate=None).startswith(prefix)


def test_ambiguous_with_one_severity_absent_is_conservative(client, synthetic_kb, real_indicators, monkeypatch):
    rec = next(r for r in real_indicators if r["id"] == "skoda_gas.engine_control_petrol")
    del rec["severity"]
    synthetic_kb(indicators=real_indicators)
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or luna.Outcome("picked", "esc_active", "r"))
    reply = _post(client, "check engine light on and esc light flashing")
    assert calls == [], calls
    assert reply.startswith("Multiple possible warning lights"), reply
    assert "Vehicle operational." not in reply.splitlines(), reply
    assert "do not drive" in reply.lower(), reply


_NOT_RECORD_VERDICTS = [
    "Drive with caution.", "Vehicle operational.", "Conditional - check first before driving.",
    "נסיעה בזהירות.", "הרכב תקין לנסיעה.", "נסיעה מותנית - בדוק תחילה.",
]


@pytest.mark.parametrize("text,language", [
    ("נורת קירור דולקת והמנוע מתחמם", "he-IL"),
    ("coolant warning and engine light on, engine overheating", "en-US"),
    ("ESC and front assist lights both stay on", "en-US"),
    ("coolant overheating and brake fluid warning", "en-US"),
])
def test_excluded_state_with_another_light_is_conservative(client, monkeypatch, text, language):
    calls = []
    monkeypatch.setattr(luna, "select", lambda question, context, options, deadline=None:
                        calls.append([o.id for o in options]) or luna.Outcome("picked", options[0].id, "r"))
    reply = _post(client, text, language=language)
    assert calls == [], calls
    if language == "he-IL":
        assert reply.startswith("זוהו מספר נורות אפשריות:"), reply
        assert "אין לנסוע עד לבירור" in reply, reply
    else:
        assert reply.startswith("Multiple possible warning lights identified:"), reply
        assert "do not drive" in reply.lower(), reply
    lines = reply.splitlines()
    for verdict in _NOT_RECORD_VERDICTS:
        assert verdict not in lines, reply
    assert "Manufacturer source" not in reply and "מקור יצרן" not in reply, reply


@pytest.mark.parametrize("text,language,prefix", [
    ("ESC light is on", "en-US", "We could not identify the dashboard light"),
    ("traction control light lit", "en-US", "We could not identify the dashboard light"),
    ("נורת יציבות נשארת דולקת", "he-IL", "לא הצלחנו לזהות את הנורה"),
])
def test_esc_without_state_gets_no_record_verdict(client, text, language, prefix):
    assert _post(client, text, language=language).startswith(prefix)


def test_esc_flashing_constantly_gets_record_verdict(client):
    reply = _post(client, "ESC light flashes constantly in corners")
    assert "Vehicle operational." in reply.splitlines(), reply


@pytest.mark.parametrize("text,language,prefix", [
    ("\u0160koda Enyaq ESC light flashing", "en-US", "No information found for the specified vehicle."),
    ("Skoda Octavia iV check engine light", "en-US", "No information found for the specified vehicle."),
    ("סקודה אלרוק נורת יציבות מהבהבת", "he-IL", "לא נמצא מידע עבור כלי הרכב"),
])
def test_more_non_petrol_skoda_is_rejected(client, text, language, prefix):
    assert _post(client, text, language=language, plate=None).startswith(prefix)


@pytest.mark.parametrize("make", ["BMW", "Toyota RAV4", "XPeng G6"])
def test_unsupported_english_makes_get_vehicle_escalate(client, make):
    reply = _post(client, f"my {make} has a brake fluid warning", plate=None)
    assert reply.startswith("No information found for the specified vehicle."), reply
