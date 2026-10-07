"""
Tool tests: identify_vehicle, vehicle_from_text, lookup_indicator and
drivability_assessment for all 6 indicators, and the rulebook pairing.
"""
import json
import re
from pathlib import Path

import pytest

import app.tools as tools_mod
from app import kb
from app.match import INDICATOR_TO_PROCEDURE
from app.tools import (
    _CASE_SENSITIVE_MAKES,
    _HE_TO_MAKE,
    _SKODA_GAS_MODELS,
    _he_pattern,
    drivability_assessment,
    get_fleet_procedure,
    identify_vehicle,
    lookup_indicator,
    mentions,
    vehicle_for_request,
    vehicle_from_text,
)

_CAPITALISED = ("Jeep", "Ford", "Merc", "Alfa")


# KB sanity
def test_kb_indicator_count():
    assert len(kb.indicators()) == 6

def test_kb_procedure_count():
    assert len(kb.procedures()) == 6

def test_kb_qualified_brake_fluid():
    rec = kb.indicator("skoda_gas.brake_fluid_low")
    assert rec is not None
    assert rec["severity"] == "stop_now"
    assert rec["hard_stop"] is True

# identify_vehicle
def test_identify_skoda_ice():
    r = identify_vehicle("Skoda", "ice")
    assert r["platform_cell"] == "skoda_gas"

def test_identify_skoda_gas():
    r = identify_vehicle("Skoda", "gas")
    assert r["platform_cell"] == "skoda_gas"

def test_identify_skoda_petrol():
    r = identify_vehicle("Skoda", "petrol")
    assert r["platform_cell"] == "skoda_gas"

def test_identify_skoda_none_powertrain():
    r = identify_vehicle("Skoda")
    assert r["platform_cell"] == "skoda_gas"

def test_identify_skoda_he():
    r = identify_vehicle("סקודה")
    assert r["platform_cell"] == "skoda_gas"

def test_identify_tesla_ev():
    r = identify_vehicle("Tesla", "ev")
    assert r["platform_cell"] is None

def test_identify_toyota():
    r = identify_vehicle("Toyota")
    assert r["platform_cell"] is None

# vehicle_from_text
def _reason(text):
    return vehicle_from_text(text)["reason"]


def test_resolve_no_make_token():
    assert _reason("the red brake light is on") == "vehicle_unknown"

def test_resolve_tesla_en():
    assert _reason("my Tesla shows a warning light") == "non_supported_vehicle"

def test_resolve_skoda_he():
    assert _reason("בסקודה שלי נדלקה נורת בלמים") == "vehicle_unknown"

def test_resolve_empty():
    assert _reason("") == "vehicle_unknown"

# lookup_indicator
_ALL_6 = [
    ("brake_fluid_low",             "stop_now",  True),
    ("brake_abs_fault",             "stop_now",  True),
    ("coolant_level_low",           "urgent",    False),
    ("engine_control_petrol",       "caution",   False),
    ("front_assist_collision_warning", "caution", False),
    ("esc_active",                  "info",      False),
]

@pytest.mark.parametrize("bare_id,severity,hard_stop", _ALL_6)
def test_lookup_bare_all6(bare_id, severity, hard_stop):
    rec = lookup_indicator(bare_id, "skoda_gas")
    assert rec is not None, f"lookup failed for {bare_id}"
    assert rec["severity"] == severity, f"severity mismatch for {bare_id}"
    assert rec["hard_stop"] is hard_stop, f"hard_stop mismatch for {bare_id}"
    # Provenance must have manual_id and source_page
    prov = rec.get("provenance", {})
    assert prov.get("manual_id"), f"missing manual_id for {bare_id}"
    assert prov.get("source_page"), f"missing source_page for {bare_id}"

def test_lookup_already_qualified():
    rec = lookup_indicator("skoda_gas.brake_fluid_low", "skoda_gas")
    assert rec is not None
    assert rec["severity"] == "stop_now"

def test_lookup_wrong_cell_returns_none():
    rec = lookup_indicator("brake_fluid_low", "toyota_hybrid")
    assert rec is None

# get_fleet_procedure

@pytest.mark.parametrize("bare_id", [b for b, *_ in _ALL_6])
def test_fleet_procedure_paired(bare_id):
    proc_id = INDICATOR_TO_PROCEDURE.get(bare_id)
    assert proc_id is not None, f"no rulebook pairing for {bare_id}"
    proc = get_fleet_procedure(proc_id)
    assert proc is not None, f"rulebook procedure {proc_id} not found"
    assert "light_name_he" in proc
    assert "explanation_he" in proc
    assert "recovery_action_he" in proc

# drivability_assessment
_DA_CASES = [
    ("brake_fluid_low",              "not_drivable",    "stop_now"),
    ("brake_abs_fault",              "not_drivable",    "stop_now"),
    ("coolant_level_low",            "drivable_caution", "conditional"),
    ("engine_control_petrol",        "drivable_caution", "caution"),
    ("front_assist_collision_warning","drivable_caution","caution"),
    ("esc_active",                   "drivable",        "info"),
]

@pytest.mark.parametrize("bare_id,status,verdict", _DA_CASES)
def test_drivability_all6(bare_id, status, verdict):
    rec = lookup_indicator(bare_id, "skoda_gas")
    assert rec is not None
    da = drivability_assessment(rec)
    assert da["status"] == status, f"status mismatch for {bare_id}: {da}"
    assert da["verdict"] == verdict, f"verdict mismatch for {bare_id}: {da}"

def test_drivability_stop_now_has_message_text():
    rec = lookup_indicator("brake_fluid_low", "skoda_gas")
    da = drivability_assessment(rec)
    assert da["message_text"] is not None
    assert "Stop" in da["message_text"] or "stop" in da["message_text"]

def test_drivability_brake_abs_stop_now():
    rec = lookup_indicator("brake_abs_fault", "skoda_gas")
    da = drivability_assessment(rec)
    assert da["status"] == "not_drivable"
    assert da["verdict"] == "stop_now"
    assert da["hard_stop"] is True

def test_drivability_unknown_conservative():
    fake_rec = {"id": "skoda_gas.fake", "severity": "unknown", "hard_stop": False}
    da = drivability_assessment(fake_rec)
    assert da["status"] == "not_drivable"
    assert da["verdict"] == "escalate"

def test_identify_skoda_superb_non_gas_model():
    assert identify_vehicle("Skoda", model="Superb")["platform_cell"] is None

def test_identify_skoda_kamiq_gas_model():
    assert identify_vehicle("Skoda", model="Kamiq")["platform_cell"] == "skoda_gas"

def test_resolve_skoda_superb_escalate():
    assert _reason("Skoda Superb brake warning") == "non_supported_vehicle"

def test_resolve_skoda_kamiq_gas():
    assert vehicle_from_text("my Skoda Kamiq petrol brake warning")["platform_cell"] == "skoda_gas"


@pytest.mark.parametrize("model", ["Scala", "Octavia", "Fabia", "Karoq"])
def test_identify_skoda_covered_models(model):
    assert identify_vehicle("Skoda", model=model)["platform_cell"] == "skoda_gas"


@pytest.mark.parametrize("text", ["Skoda Enyaq ESC light", "Skoda Octavia diesel check engine light", "סקודה סופרב נורת מנוע"])
def test_resolve_non_petrol_skoda_is_unsupported(text):
    assert _reason(text) == "non_supported_vehicle"


@pytest.mark.parametrize("text", ["Skoda Octavia petrol check engine light", "סקודה אוקטביה בנזין נורת מנוע"])
def test_resolve_petrol_skoda_still_resolves(text):
    assert vehicle_from_text(text)["platform_cell"] == "skoda_gas"


@pytest.mark.parametrize("text", [
    "\u0160koda Enyaq ESC light",
    "\u0161koda superb brake warning",
    "Skoda Octavia iV check engine light",
    "Skoda Citigo e iV ESC light",
    "Skoda Rapid check engine light",
    "Skoda Octavia electric check engine light",
    "Skoda Octavia G-TEC check engine light",
    "Skoda Octavia CNG check engine light",
    "סקודה אלרוק נורת יציבות",
    "סקודה אנייק נורת יציבות",
    "סקודה אוקטביה חשמלית נורת מנוע",
    "סקודה אוקטביה גז טבעי נורת מנוע",
    "סקודה ראפיד נורת מנוע",
    "סקודה סיטיגו נורת מנוע",
])
def test_resolve_more_non_petrol_skoda_is_unsupported(text):
    assert _reason(text) == "non_supported_vehicle"


@pytest.mark.parametrize("text", [
    "Skoda Octavia petrol, I plug in my phone and the check engine light comes on",
    "Skoda Octavia mild hybrid check engine light",
    "Skoda Octavia mild-hybrid e-TEC check engine light",
])
def test_resolve_petrol_skoda_phrasings_resolve(text):
    assert vehicle_from_text(text)["platform_cell"] == "skoda_gas"


@pytest.mark.parametrize("text", [
    "Skoda Octavia IV petrol check engine light",
    "סקודה אוקטביה בנזין לחצתי על הגז ונדלקה נורת מנוע",
])
def test_resolve_petrol_controls(text):
    assert vehicle_from_text(text)["platform_cell"] == "skoda_gas"


# A Skoda model named without the make decides the cell too.
@pytest.mark.parametrize("text", [
    "Enyaq ESC light",
    "Elroq ESC light flashing",
    "Kodiaq brake warning",
    "my Superb has the check engine light on",
    "אניאק נורת יציבות",
])
def test_resolve_unsupported_skoda_model_without_make(text):
    assert _reason(text) == "non_supported_vehicle"


@pytest.mark.parametrize("text", ["Octavia check engine light", "Kamiq ESC light"])
def test_resolve_supported_skoda_model_without_make(text):
    assert _reason(text) == "vehicle_unknown"



def test_no_default_cell_remains():
    assert not [name for name in dir(tools_mod) if "DEFAULT" in name]
    assert vehicle_from_text("brake warning")["platform_cell"] is None


G, NS, VU, MM = "skoda_gas", "non_supported_vehicle", "vehicle_unknown", "vehicle_mismatch"
_SOP = "Skoda Octavia petrol "
_HE_SOP = "סקודה אוקטביה בנזין"
_VEHICLE_ROWS = [
    # Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
    ("RED BRAKE WARNING LIGHT, CALL ME 555 0123", VU), ("RED BRAKE WARNING LIGHT, ODOMETER 120 000 KM", VU),
    ("red brake warning light", VU), ("Skoda red brake warning light", VU), ("Octavia check engine light", VU),
    ("Trabant check engine light", VU), ("Skoda Octavia check engine light", VU),
    ("Skoda Octavia petrol check engine light", G), ("סקודה אוקטביה נורת מנוע", VU), (_HE_SOP + " נורת מנוע", G),
    ("Skoda Octavia diesel check engine light", NS), ("Enyaq ESC light", NS), ("BMW check engine light", NS),
    ("my bmw has the brake warning on", NS), ("Mercedes-Benz brake fluid warning", NS),
    ("Tesla Model 3 check engine light", NS), ("Toyota RAV4 check engine light", NS),
    ("Jaecoo 7 brake warning", NS), ("XPeng G6 ESC light", NS), ("Kia Picanto check engine light", NS),
    ("", VU),
    ("Skoda Octavia mild hybrid check engine light", G), ("Skoda Felicia petrol check engine light", VU),
    ("my BMW replaced my Skoda Octavia petrol; check engine light", NS), (_HE_SOP + ", וגם BMW, נורת מנוע", NS),
    ("נדלקה נורת מנוע בBMW שלי", NS), ("הTesla שלי עם נורת בלמים", NS), ("סקודה הSuperb נורת מנוע", NS),
    ("יש לי סקודה, בEnyaq נדלקה נורת בלמים", NS), ("Land  Rover check engine light", NS),
    ("Alfa\tRomeo check engine light", NS), ("Lynk&Co check engine light", NS), ("Lynk and Co check engine light", NS),
    ("x peng brake warning", NS), ("B.M.W check engine light", NS), ("Range Rover check engine light", NS),
    ("ביואיידי אטו 3 נורת בלמים", NS), ("בי.וו.די נורת בלמים", NS), ("Aiways U5 brake warning", NS),
    ("my Seat Ibiza brake warning", NS), ("MG ZS check engine light", NS),
    ("Skoda Octavia petrol, check engine light came on at 45000 km", G),
    ("km 12345 Skoda Octavia petrol check engine light", G),
    ("No 12345, Skoda Octavia petrol, check engine light", G),
    # Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
    ("call me on 555-01 23, Skoda Octavia petrol check engine light", G),
    ("סקודה אוקטביה הiV נורת מנוע", NS), ("Skoda Octavia iV check engine light", NS),
    ("Skoda Octavia petrol, I plug in my phone and the check engine light comes on", G),
    ("Skoda Octavia plug-in check engine light", NS), ("MG4 check engine light", NS), ("my mg zs brake warning", VU),
    ("Red brake warning light. Seat heater also off", NS),
] + [(_SOP + tail, G) for tail in (
    "check engine light באוקספורד", "check engine light after taking 5 mg medication",
    "check engine light after fitting a car seat", "the genesis of the fault is unclear, check engine light",
    "I had to dodge a pothole, check engine light",
)] + [("Skoda Octavia IV petrol check engine light", G)] + [
    (f"{_HE_SOP}, {verb} בצד הדרך עם נורת מנוע", G) for verb in ("ונשארנו", "חזרנו", "אמרנו", "עברנו", "גררנו")
] + [(f"{_HE_SOP} נורת מנוע {word}", G) for word in ("שרנו", "כמעט במוות", "בסקאלה של 1 עד 10", "בסיאטל", "ליד רנואר")] + [
    ("a jeep hit me, red brake warning light", VU),
    ("Jeep Compass check engine light", NS),
    ("superb red brake warning light", VU),
    ("skoda superb petrol red brake warning light", NS),
    ("skoda octavia petrol or superb, red brake warning light", VU),
    ("Skoda Octavia petrol, rapid flashing check engine light", G),
    ("skoda-superb petrol red brake warning light", NS),
]


@pytest.mark.parametrize("text,expected", _VEHICLE_ROWS)
def test_vehicle_from_text(text, expected):
    result = vehicle_from_text(text)
    assert (result["platform_cell"] or result["reason"]) == expected
    if expected == G:
        assert result["platform_cell"] == G and result["reason"] is None
    else:
        assert result["platform_cell"] is None


@pytest.mark.parametrize("text,term,hit", [
    ("בסיאט", "סיאט", True), ("בסיאטל", "סיאט", False), ("ברנו", "רנו", True), ("שרנו", "רנו", False),
    ("רנואר", "רנו", False),
])
def test_he_pattern(text, term, hit):
    assert bool(re.search(_he_pattern(term), text)) is hit


def test_identify_bmw_is_a_known_unsupported_make():
    assert identify_vehicle("BMW")["reason"] == "not_skoda_gas"


def test_every_known_make_other_than_skoda_escalates():
    for make in tools_mod._known_makes():
        if make == "Skoda":
            continue
        texts = [f"{make} check engine light", f"{make.upper()}: ESC light"]
        if make not in _CASE_SENSITIVE_MAKES and make not in _CAPITALISED:
            texts.append(f"my {make.lower()} brake warning")
        for text in texts:
            assert _reason(text) == "non_supported_vehicle", text


@pytest.mark.parametrize("make", sorted(_CASE_SENSITIVE_MAKES))
def test_weak_makes_in_lower_case_are_not_recognised(make):
    assert _reason(f"my {make.lower()} brake warning") == "vehicle_unknown"


def test_capitalised_makes_are_pinned():
    assert tools_mod._CAPITALISED_MAKES == set(_CAPITALISED)
    assert not tools_mod._CAPITALISED_MAKES & _CASE_SENSITIVE_MAKES
    assert tools_mod._CAPITALISED_MAKES <= set(tools_mod._known_makes()) | set(tools_mod._EN_ALIASES)


def test_capitalised_skoda_models_are_pinned():
    assert tools_mod._CAPITALISED_SKODA_MODELS == {"Superb"}
    assert "superb" not in tools_mod._SKODA_OTHER_MODELS
    assert tools_mod._SKODA_OTHER_MODELS["skoda superb"] == "Superb"


@pytest.mark.parametrize("name", _CAPITALISED)
def test_capitalised_makes_in_lower_case_are_not_recognised(name):
    assert _reason(f"my {name.lower()} brake warning") == "vehicle_unknown"
    assert vehicle_for_request(f"my {name.lower()} red brake warning light", plate="FH-104-27")["reason"] is None


@pytest.mark.parametrize("name", _CAPITALISED)
def test_capitalised_makes_as_written_still_count(name):
    assert _reason(f"my {name} brake warning") == "non_supported_vehicle"
    assert _reason(f"{name.upper()}: ESC light") == "non_supported_vehicle"
    assert vehicle_for_request(f"my {name} red brake warning light", plate="FH-104-27")["reason"] == "vehicle_mismatch"


# The 29 makes other than Skoda that main recognised before the catalog was rebuilt.
_MAIN_MAKES = (
    "Aiways", "BYD", "Changan", "Chery", "Chevrolet", "Citroen", "Dacia", "Fiat", "Geely", "Genesis", "Honda",
    "Hyundai", "Isuzu", "Jaecoo", "KGM", "Kia", "MG", "Mazda", "Mitsubishi", "Nissan", "Opel", "Peugeot",
    "Renault", "Seat", "Subaru", "Tesla", "Toyota", "Volkswagen", "Zeekr",
)


@pytest.mark.parametrize("name", _MAIN_MAKES)
def test_every_make_main_escalates_today_still_escalates(name):
    assert vehicle_from_text(f"{name} check engine light")["reason"] == "non_supported_vehicle"


def test_every_hebrew_alias_names_a_known_make():
    assert set(_HE_TO_MAKE.values()) <= set(tools_mod._known_makes())


def test_every_catalog_make_has_a_hebrew_alias():
    assert {m["make"] for m in kb.fleet_models()} <= set(_HE_TO_MAKE.values())


@pytest.mark.parametrize("name", ["Scala", "Fabia", "Karoq"])
def test_petrol_sheet_models_are_recognised_without_the_catalog(name):
    assert mentions(f"my {name} brake warning")["models"] == {name}


def _knowledge(name):
    with open(Path(kb._resolve_root()) / "knowledge" / name, encoding="utf-8") as f:
        return json.load(f)


_RANKING = [
    (1, "Jaecoo", "7", 15230), (2, "Hyundai", "Kona", 11770), (3, "Toyota", "Corolla Cross", 9002),
    (4, "Chery", "Tiggo 8 Pro", 8187), (5, "Kia", "Picanto", 8062), (6, "Toyota", "Yaris Cross", 7885),
    (7, "Chery", "FX", 7522), (8, "Toyota", "RAV4", 7178), (9, "Hyundai", "Elantra", 6072),
    (10, "BYD", "Seal U", 5691), (11, "Hyundai", "Venue", 5554), (12, "BYD", "Atto 3", 5496),
    (13, "Skoda", "Octavia", 5339), (14, "Kia", "Niro", 5319), (15, "XPeng", "G6", 4844),
    (16, "Chery", "Tiggo 7 Pro", 4716), (17, "Mazda", "CX-5", 4425), (18, "Skoda", "Kamiq", 4354),
    (19, "Kia", "Sportage", 4334), (20, "Hyundai", "Tucson", 4297),
]
_SOURCE_URLS = [
    "https://www.cartube.co.il/חדשות-רכב/שיא-מסירות-של-כל-הזמנים-מסירות-רכב-חדש-בישראל-סיכום-2025",
    "https://www.auto.co.il/articles/car-news/local-news/cars-sales-2025/",
    "https://bestsellingcarsblog.com/2026/01/israel-full-year-2025-jaecoo-7-takes-the-lead-chinese-at-29-7-share-peak-of-49-5-in-september/",
]


def test_fleet_catalog_is_the_public_2025_top_20():
    catalog = _knowledge("fleet-catalog.json")
    models, meta = catalog["models"], catalog["_meta"]
    assert [m["rank"] for m in models] == list(range(1, 21))
    assert len({m["make"] for m in models}) == 9 == meta["distinct_makes"]
    assert all(set(m) == {"rank", "make", "model", "units_2025"} for m in models)
    units = [m["units_2025"] for m in models]
    assert units == sorted(units, reverse=True)
    assert len(meta["sources"]) == 3
    assert all(set(source) == {"name", "url", "period"} for source in meta["sources"])
    assert "same shape" not in json.dumps(catalog, ensure_ascii=False)
    skoda = {m["model"].lower() for m in models if m["make"] == "Skoda"}
    assert skoda and skoda <= _SKODA_GAS_MODELS


def test_fleet_catalog_rows_match_the_published_ranking():
    catalog = _knowledge("fleet-catalog.json")
    assert [(m["rank"], m["make"], m["model"], m["units_2025"]) for m in catalog["models"]] == _RANKING
    assert [source["url"] for source in catalog["_meta"]["sources"]] == _SOURCE_URLS
    assert {source["period"] for source in catalog["_meta"]["sources"]} == {"January-December 2025"}


def test_icon_rulebook_has_no_page_fields():
    rulebook = _knowledge("example-icon-rulebook.json")
    assert all("page" not in procedure for procedure in rulebook["procedures"])
    assert rulebook["_meta"]["source"] == "Synthetic example written for this repository; every entry is invented."


def test_icon_rulebook_fits_the_demo_company_and_its_dispatch_actions():
    rulebook = _knowledge("example-icon-rulebook.json")
    ids = [procedure["id"] for procedure in rulebook["procedures"]]
    assert ids == ["engine-epc", "coolant-level", "brake-fluid", "brake-abs", "driver-assist", "washer-fluid-low"]
    assert set(INDICATOR_TO_PROCEDURE.values()) <= set(ids)
    assert _knowledge("company-policy.json")["company"] in rulebook["_meta"]["title_en"]
    named = [re.search(r"\(dispatch action (\w+)\)$", gloss) for gloss in rulebook["_meta"]["disposition_vocab"].values()]
    assert all(named), named
    assert {m.group(1) for m in named} == {"roadside_rescue", "tow_to_dealer", "dealer_appointment"}
    assert {m.group(1) for m in named} <= set(_knowledge("dispatch-model.json")["actions"])
    bases = {re.sub(r" \(dispatch action \w+\)$", "", gloss) for gloss in rulebook["_meta"]["disposition_vocab"].values()}
    actions = {row["action"] for p in rulebook["procedures"] for row in p.get("recovery_conditional", [])}
    actions |= {v["action"] for p in rulebook["procedures"] for v in p.get("color_variants", {}).values()}
    assert actions <= bases, sorted(actions - bases)


@pytest.mark.parametrize("skoda", ["\u0160koda", "S\u030ckoda"])
def test_caron_spelled_skoda_resolves(skoda):
    assert vehicle_from_text(f"{skoda} Octavia petrol check engine light")["platform_cell"] == "skoda_gas"
