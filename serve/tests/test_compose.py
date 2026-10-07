"""
Composer tests: rulebook section before the carmind section, severity and message
text verbatim, provenance present, he/en labels, ambiguous replies and the
stop-level precaution, and no "safe to drive" anywhere.
"""
import pytest

from app.compose import (
    build_indicator_reply,
    compose_ambiguous,
    compose_escalate,
    compose_reply,
)
from app.match import INDICATOR_TO_PROCEDURE
from app.tools import drivability_assessment, get_fleet_procedure, lookup_indicator

FORBIDDEN_PHRASES = ["safe to drive", "בטוח לנסיעה"]


def _no_forbidden(text: str):
    lower = text.lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase.lower() not in lower, f"Forbidden phrase found: {phrase!r}"


# brake_fluid_low (stop_now / hard_stop)
def test_brake_fluid_he_structure():
    reply = build_indicator_reply("brake_fluid_low", "skoda_gas", "he-IL")
    # Rulebook section before carmind section
    pos_a = reply.find("מדריך הצי")
    pos_b = reply.find("הערכת carmind")
    assert pos_a >= 0, "Missing Section A label"
    assert pos_b >= 0, "Missing Section B label"
    assert pos_a < pos_b, "Section A must come before Section B"
    # Attribution
    assert "מקור: נוהל הצי" in reply
    # Severity verbatim
    assert "stop_now" in reply
    # Provenance page 127
    assert "127" in reply
    # Verbatim message_text
    assert "Stop driving" in reply
    # Do-not-drive
    assert "אין לנסוע" in reply
    _no_forbidden(reply)


def test_brake_fluid_en_structure():
    reply = build_indicator_reply("brake_fluid_low", "skoda_gas", "en-US")
    assert "Fleet recovery guide:" in reply
    assert "carmind assessment:" in reply
    assert reply.index("Fleet recovery guide:") < reply.index("carmind assessment:")
    assert "Source: fleet rulebook" in reply
    assert "stop_now" in reply
    assert "127" in reply
    assert "Do not drive." in reply
    _no_forbidden(reply)


def test_brake_fluid_message_text_verbatim():
    rec = lookup_indicator("brake_fluid_low", "skoda_gas")
    da = drivability_assessment(rec)
    proc = get_fleet_procedure(INDICATOR_TO_PROCEDURE["brake_fluid_low"])
    reply = compose_reply(rec, da, proc, "en-US")
    # Verbatim message_text from record
    assert "Stop driving! Seek the help of a specialist garage." in reply
    _no_forbidden(reply)


# brake_abs_fault (also stop_now / hard_stop)
def test_brake_abs_no_safe_to_drive():
    reply = build_indicator_reply("brake_abs_fault", "skoda_gas", "he-IL")
    _no_forbidden(reply)
    assert "אין לנסוע" in reply


# coolant_level_low (urgent / not hard_stop)
def test_coolant_he():
    reply = build_indicator_reply("coolant_level_low", "skoda_gas", "he-IL")
    assert "מדריך הצי" in reply
    assert "הערכת carmind" in reply
    assert "urgent" in reply
    _no_forbidden(reply)
    # No unconditional do-not-drive line
    assert "אין לנסוע." not in reply


def test_coolant_en_provenance():
    reply = build_indicator_reply("coolant_level_low", "skoda_gas", "en-US")
    assert "142" in reply  # source_page for coolant


# engine_control_petrol (caution)
def test_engine_caution():
    reply = build_indicator_reply("engine_control_petrol", "skoda_gas", "en-US")
    assert "caution" in reply
    _no_forbidden(reply)


# esc_active (info)
def test_esc_info_no_forbidden():
    reply = build_indicator_reply("esc_active", "skoda_gas", "en-US")
    _no_forbidden(reply)
    assert "info" in reply


# compose_ambiguous
def _get_recs(bare_ids):
    return [lookup_indicator(b, "skoda_gas") for b in bare_ids]


def test_ambiguous_coolant_engine_he():
    recs = _get_recs(["coolant_level_low", "engine_control_petrol"])
    reply = compose_ambiguous(recs, "he-IL")
    assert "קירור" in reply, f"Hebrew coolant name missing: {reply!r}"
    assert "מנוע" in reply, f"Hebrew engine name missing: {reply!r}"
    assert "coolant warning" not in reply.lower()
    assert "engine electronics" not in reply.lower()
    # No verdict
    _no_forbidden(reply)
    # No single verdict line
    assert "אין לנסוע" not in reply
    assert "Do not drive" not in reply


def test_ambiguous_coolant_engine_en():
    recs = _get_recs(["coolant_level_low", "engine_control_petrol"])
    reply = compose_ambiguous(recs, "en-US")
    assert "Coolant" in reply
    assert "safe to drive" not in reply.lower()
    # No single verdict
    assert "Do not drive." not in reply


def test_ambiguous_stop_level_has_precaution():
    # brake_fluid_low is stop_now - precaution must appear
    recs = _get_recs(["brake_fluid_low", "coolant_level_low"])
    for lang in ("he-IL", "en-US"):
        reply = compose_ambiguous(recs, lang)
        _no_forbidden(reply)
        has_precaution = (
            "אין לנסוע" in reply
            or "do not drive" in reply.lower()
            or "serious fault" in reply.lower()
            or "תקלה חמורה" in reply
        )
        assert has_precaution, f"Missing precaution for stop-level ambiguous ({lang}): {reply!r}"


def test_ambiguous_no_verdict_stop_level():
    recs = _get_recs(["brake_fluid_low", "coolant_level_low"])
    reply = compose_ambiguous(recs, "en-US")
    # No conclusive single-indicator verdict
    assert "Manufacturer source" not in reply
    assert "skoda_kamiq" not in reply


# escalate
def test_escalate_he():
    reply = compose_escalate("he-IL", "no_match")
    _no_forbidden(reply)
    assert len(reply) > 5

def test_escalate_en():
    reply = compose_escalate("en-US", "non_supported_vehicle")
    _no_forbidden(reply)
    assert "specialist" in reply.lower()


@pytest.mark.parametrize("language", ["he-IL", "en-US", "ar-SA", "ru-RU"])
def test_vehicle_unknown_and_mismatch_texts(language):
    unknown = compose_escalate(language, "vehicle_unknown")
    mismatch = compose_escalate(language, "vehicle_mismatch")
    if language == "he-IL":
        assert unknown.startswith("carmind צריך לדעת מהו הרכב: יש להזין את מספר הרישוי בשדה מספר הרישוי.")
        assert mismatch.startswith("הרכב שברשומות והרכב שתואר אינם תואמים")
    else:
        assert unknown.startswith("carmind needs the vehicle: enter its plate number in the plate number field")
        assert mismatch.startswith("The car on record and the vehicle described do not match")


@pytest.mark.parametrize("language", ["he-IL", "en-US"])
def test_light_name_matches_the_ambiguous_names(language):
    from app import kb
    from app.compose import light_name
    prefix = "זוהו מספר נורות אפשריות: " if language == "he-IL" else "Multiple possible warning lights identified: "
    for rec in kb.indicators().values():
        first_line = compose_ambiguous([rec], language).split("\n")[0]
        assert first_line == prefix + light_name(rec, language) + ".", rec["id"]


def test_contract_choice_line():
    from app.compose import contract_choice_line
    names = ["Coolant level too low", "Petrol engine control faulty"]
    assert contract_choice_line("picked", "Coolant level too low", names, ("coolant",), "en-US") == (
        "Light picked by a language model from the matched lights (Coolant level too low, Petrol engine control "
        "faulty): Coolant level too low. Words in the description that match it: coolant. The verdict is that "
        "light's own record.")
    assert contract_choice_line("asked", None, names, (), "en-US") == (
        "A language model could not pick one of the matched lights (Coolant level too low, Petrol engine control "
        "faulty), so the caller is asked.")
    assert contract_choice_line("picked", "א", ["א", "ב"], ("x", "y"), "he-IL") == (
        "נורה שמודל שפה בחר מבין הנורות שזוהו (א, ב): א. מילים בתיאור שמתאימות לה: x, y. ההערכה היא של הרשומה של נורה זו.")
    assert contract_choice_line("asked", None, ["א", "ב"], (), "he-IL") == (
        "מודל שפה לא בחר אחת מהנורות שזוהו (א, ב), ולכן יש לשאול את הפונה.")
    for language in ("ar-SA", "ru-RU"):
        assert contract_choice_line("asked", None, names, (), language) == contract_choice_line(
            "asked", None, names, (), "en-US")
        assert contract_choice_line("picked", names[0], names, ("coolant",), language) == contract_choice_line(
            "picked", names[0], names, ("coolant",), "en-US")
    for status in ("declined", "rejected", "failed", "x"):
        assert contract_choice_line(status, names[0], names, ("coolant",), "en-US") == ""
    assert contract_choice_line("asked", None, ["A", "B", "A"], (), "en-US") == (
        "A language model could not pick one of the matched lights (A, B), so the caller is asked.")


def test_agent_choice_line():
    from app.compose import agent_choice_line
    names = ["A", "B"]
    assert agent_choice_line("picked", "A", names, "the words say A", False) == (
        "Light picked by a language model from the matched lights (A, B): A. The verdict is that light's own record; "
        "the dispatch is the more cautious of the plan for this light and the plan for an unconfirmed light. "
        "Model reasoning (not a verdict): the words say A")
    assert agent_choice_line("asked", None, names, "both named", False) == (
        "A language model could not pick one of the matched lights (A, B). Model reasoning (not a verdict): both named")
    assert agent_choice_line("asked", None, names, "", True) == (
        "A language model could not pick one of the matched lights (A, B). Model reasoning (not a verdict): "
        "withheld by carmind's word and character filter.")
    assert agent_choice_line("asked", None, names, "", False) == (
        "A language model could not pick one of the matched lights (A, B). Model reasoning (not a verdict): "
        "none given.")
    for status in ("rejected", "failed", "declined", "x"):
        assert agent_choice_line(status, "A", names, "r", False) == ""


def test_contract_flags_line():
    from app.compose import contract_flags_line
    assert contract_flags_line(["repeat_problem"], "en-US") == (
        "Context inferred by a language model from the description: repeat_problem. Context can only make the "
        "verdict more cautious.")
    assert contract_flags_line(["fresh_handover", "repeat_problem"], "he-IL") == (
        "הקשר שמודל שפה הסיק מהתיאור: fresh_handover, repeat_problem. הקשר יכול רק להחמיר את ההערכה.")
    assert contract_flags_line([], "en-US") == ""
    assert contract_flags_line([], "he-IL") == ""


def test_agent_flags_line():
    from app.compose import agent_flags_line
    head = ("Context inferred by a language model from the description: repeat_problem. Context can only make the "
            "verdict more cautious. Model reasoning (not a verdict): ")
    assert agent_flags_line(["repeat_problem"], "it came back", False) == head + "it came back"
    assert agent_flags_line(["repeat_problem"], "", True) == head + "withheld by carmind's word and character filter."
    assert agent_flags_line(["repeat_problem"], "", False) == head + "none given."
    assert agent_flags_line([], "r", False) == ""
