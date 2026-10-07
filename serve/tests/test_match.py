"""
Keyword matcher: every keyword on its own reaches its indicator.

The keywords are written out here rather than read from match.py, so that a lost or
misspelled keyword is caught.
"""
import pytest

from app.match import match

_KEYWORDS = [
    ("brake fluid", "brake_fluid_low"),
    ("red brake", "brake_fluid_low"),
    ("brake warning", "brake_fluid_low"),
    ("brake system", "brake_fluid_low"),
    ("בלם", "brake_fluid_low"),
    ("abs", "brake_abs_fault"),
    ("anti-lock", "brake_abs_fault"),
    ("anti lock", "brake_abs_fault"),
    ("coolant", "coolant_level_low"),
    ("cooling", "coolant_level_low"),
    ("קירור", "coolant_level_low"),
    ("check engine", "engine_control_petrol"),
    ("engine control", "engine_control_petrol"),
    ("epc", "engine_control_petrol"),
    ("engine light", "engine_control_petrol"),
    ("petrol engine", "engine_control_petrol"),
    ("מנוע", "engine_control_petrol"),
    ("front assist", "front_assist_collision_warning"),
    ("collision", "front_assist_collision_warning"),
    ("התנגשות", "front_assist_collision_warning"),
    ("מרחק", "front_assist_collision_warning"),
    ("esc", "esc_active"),
    ("stability", "esc_active"),
    ("traction", "esc_active"),
    ("יציבות", "esc_active"),
    ("החלקה", "esc_active"),
]


@pytest.mark.parametrize("keyword,bare_id", _KEYWORDS)
def test_each_keyword_alone_matches_its_indicator(keyword, bare_id):
    text = f"{keyword} flashing" if bare_id == "esc_active" else keyword
    assert match(text) == [bare_id]


@pytest.mark.parametrize("text", [
    "ESC light stays on continuously, not flashing",
    "coolant temperature too high, engine overheating",
    "נורת יציבות דולקת באופן קבוע",
    "טמפרטורה גבוהה במערכת הקירור",
])
def test_state_the_record_excludes_drops_the_indicator(text):
    assert match(text) == []


def test_excluded_state_keeps_the_other_light():
    assert match("coolant overheating and brake fluid warning") == ["brake_fluid_low"]


@pytest.mark.parametrize("text,ids", [
    ("esc light is flashing", ["esc_active"]),
    ("coolant level low", ["coolant_level_low"]),
])
def test_record_state_still_matches(text, ids):
    assert match(text) == ids


@pytest.mark.parametrize("keyword", ["esc", "stability", "traction", "יציבות", "החלקה"])
def test_esc_keyword_alone_names_no_state(keyword):
    from app import match as match_mod
    assert match(keyword) == []
    assert match_mod.excluded(keyword) == ["esc_active"]


@pytest.mark.parametrize("text", [
    "ESC light is on",
    "ESC light came on and stayed on",
    "ESC light isn't flashing, it's just on",
    "esc light on all the time, won't go off",
    "traction control light lit",
    "נורת יציבות דולקת כל הזמן",
    "נורת יציבות נשארת דולקת",
    "ESC light flashed and then stayed on",
])
def test_esc_without_flashing_is_excluded(text):
    from app import match as match_mod
    assert match(text) == []
    assert match_mod.excluded(text) == ["esc_active"]


@pytest.mark.parametrize("text", [
    "ESC light flashes constantly in corners",
    "esc light is flashing",
    "ESC blinking on wet road",
    "נורת יציבות מהבהבת",
    "נורת החלקה מהבהבת כל הזמן",
    "הבהוב של נורת יציבות",
])
def test_esc_flashing_is_answered(text):
    from app import match as match_mod
    assert match(text) == ["esc_active"]
    assert match_mod.excluded(text) == []


@pytest.mark.parametrize("text", [
    "טמפרטורת נוזל הקירור גבוהה",
    "התחממות יתר, נורת נוזל קירור",
    "נורת קירור והרכב מתחמם",
    "נוזל קירור חם מדי",
    "coolant light, temp gauge in the red",
    "coolant warning, engine running hot",
])
def test_coolant_overheating_is_excluded(text):
    from app import match as match_mod
    assert match(text) == []
    assert match_mod.excluded(text) == ["coolant_level_low"]


def test_coolant_hot_day_is_answered():
    from app import match as match_mod
    text = "coolant light on, it's a hot day"
    assert match(text) == ["coolant_level_low"]
    assert match_mod.excluded(text) == []


@pytest.mark.parametrize("text,ids,excluded_ids", [
    ("נורת קירור דולקת והמנוע מתחמם", ["engine_control_petrol"], ["coolant_level_low"]),
    ("coolant warning and engine light on, engine overheating", ["engine_control_petrol"], ["coolant_level_low"]),
    ("ESC and front assist lights both stay on", ["front_assist_collision_warning"], ["esc_active"]),
])
def test_multi_light_with_excluded_state(text, ids, excluded_ids):
    from app import match as match_mod
    assert match(text) == ids
    assert match_mod.excluded(text) == excluded_ids


def test_keywords_in():
    from app.match import keywords_in
    assert keywords_in("coolant_level_low", "Coolant light and the cooling fan") == ["coolant", "cooling"]
    assert keywords_in("engine_control_petrol", "coolant light and check engine light both on") == [
        "check engine", "engine light"]
    assert keywords_in("coolant_level_low", "נורת נוזל קירור דולקת") == ["נוזל קירור", "קירור"]
    assert keywords_in("engine_control_petrol", "epc light") == ["epc"]
    assert keywords_in("esc_active", "coolant light") == []
    assert keywords_in("no_such_light", "coolant light") == []
