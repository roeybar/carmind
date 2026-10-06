"""
Deterministic knowledge tools. No LLM calls, no writes.
"""
import re
from typing import Optional

from app import kb

# Skoda models covered by the Skoda petrol platform sheet, as written in English text.
_SKODA_GAS_MODEL_NAMES: tuple[str, ...] = ("Kamiq", "Scala", "Octavia", "Fabia", "Karoq")
_SKODA_GAS_MODELS = {name.lower() for name in _SKODA_GAS_MODEL_NAMES}
# Hebrew spellings of the covered models. Scala is left out: "סקאלה" is also the word for a scale.
_SKODA_GAS_MODELS_HE: dict[str, str] = {
    "אוקטביה": "Octavia", "קאמיק": "Kamiq", "פאביה": "Fabia", "קארוק": "Karoq",
}

# Skoda models the petrol sheet does not cover. English matches whole words, Hebrew at the start of a word.
# "Rapid" and "superb" are also ordinary English words: in lower case they count only right after "Skoda",
# with spaces or a hyphen between (_after_skoda_pattern). Superb as written or in capitals counts anywhere.
_SKODA_OTHER_MODELS: dict[str, str] = {
    "enyaq": "Enyaq", "elroq": "Elroq", "kodiaq": "Kodiaq", "skoda superb": "Superb",
    "citigo": "Citigo", "skoda rapid": "Rapid",
    "אניאק": "Enyaq", "אנייק": "Enyaq", "אלרוק": "Elroq", "קודיאק": "Kodiaq", "סופרב": "Superb",
    "סיטיגו": "Citigo", "ראפיד": "Rapid",
}
# Skoda models whose English name is also an ordinary word: recognised anywhere only as written or
# in capitals, and then they count as a named model like any other. Without a plate, the word in any
# case keeps the text from resolving to a covered model (vehicle_from_text).
_CAPITALISED_SKODA_MODELS = {"Superb"}
# Powertrain words that rule out the petrol sheet.
_POWERTRAIN_WORDS: list[tuple[str, str]] = [
    ("diesel", "diesel"), ("tdi", "diesel"), ("דיזל", "diesel"), ("סולר", "diesel"),
    ("phev", "phev"), ("plug-in", "phev"), ("plug in hybrid", "phev"), ("פלאג", "phev"),
    ("hybrid", "hybrid"), ("היבריד", "hybrid"),
    ("ev", "ev"), ("bev", "ev"), ("electric", "ev"), ("חשמלי", "ev"),
    ("g-tec", "cng"), ("gtec", "cng"), ("g tec", "cng"), ("cng", "cng"),
    ("גז טבעי", "cng"), ("גז דחוס", "cng"),
]
# Words that name a petrol car.
_PETROL_WORDS: tuple[str, ...] = ("petrol", "gasoline", "e-tec", "בנזין")
# Mild hybrids (e-TEC) are petrol cars: these phrases count as petrol and are removed before the scan.
_MILD_HYBRID = re.compile(r"\bmild[- ]?hybrid\b|\bmhev\b|היבריד מתון|מיילד היבריד")
# The iV plug-in badge, matched on the original text: a bare "IV" is a generation number.
_IV_BADGE = re.compile(r"(?<![A-Za-z0-9])(?:iV|[eE][- ]?[iI][vV])(?![A-Za-z0-9])")

# Hebrew spellings of the makes carmind recognises.
_HE_TO_MAKE: dict[str, str] = {
    "סקודה": "Skoda",
    "קיה": "Kia",
    "פיג'ו": "Peugeot",
    "יונדאי": "Hyundai",
    "טויוטה": "Toyota",
    "טסלה": "Tesla",
    "צ'רי": "Chery",
    "סיטרואן": "Citroen",
    "סובארו": "Subaru",
    "סיאט": "Seat",
    "רנו": "Renault",
    "דאצ'יה": "Dacia",
    "מאזדה": "Mazda",
    "הונדה": "Honda",
    "אופל": "Opel",
    "מיצובישי": "Mitsubishi",
    "ניסאן": "Nissan",
    "פולקסווגן": "Volkswagen",
    "פיאט": "Fiat",
    "ג'נסיס": "Genesis",
    "ג'אקו": "Jaecoo",
    "זיקר": "Zeekr",
    "ג'ילי": "Geely",
    "צ'אנגן": "Changan",
    "איסוזו": "Isuzu",
    "שברולט": "Chevrolet",
    "אקספנג": "XPeng", "ב.מ.וו": "BMW", "מרצדס": "Mercedes", "אאודי": "Audi", "וולוו": "Volvo",
    "פורד": "Ford", "לקסוס": "Lexus", "סוזוקי": "Suzuki",
    "במוו": "BMW", "ביואיידי": "BYD", "בי.וו.די": "BYD", "בי.ווי.די": "BYD", "ג'יקו": "Jaecoo",
}

# English names of makes recognised in free text besides those in the fleet catalog. None of them
# is supported, so naming one escalates. Words that are also ordinary English (Mini, Smart, Ram)
# are left out; a make on neither list is not recognised.
_OTHER_MAKES: tuple[str, ...] = (
    "Aiways", "Alfa Romeo", "Audi", "BMW", "Cadillac", "Changan", "Chevrolet", "Chrysler", "Citroen",
    "Cupra", "Dacia", "Dodge", "Fiat", "Ford", "Geely", "Genesis", "GWM", "Haval", "Honda", "Infiniti",
    "Isuzu", "Jaguar", "Jeep", "KGM", "Land Rover", "Leapmotor", "Lexus", "Lynk & Co", "Maxus",
    "Mercedes", "Mercedes-Benz", "MG", "Mitsubishi", "NIO", "Nissan", "Omoda", "Opel", "Peugeot",
    "Polestar", "Porsche", "Renault", "Seat", "SsangYong", "Subaru", "Suzuki", "Tesla", "Volkswagen",
    "Volvo", "Zeekr",
)
# Other English spellings of recognised makes, mapped to the make's name.
_EN_ALIASES: dict[str, str] = {
    "B M W": "BMW", "X Peng": "XPeng", "Benz": "Mercedes", "Merc": "Mercedes", "Chevy": "Chevrolet",
    "Range Rover": "Land Rover", "Alfa": "Alfa Romeo", "VW": "Volkswagen",
}
# Makes that are also ordinary English words: recognised only as written or in capitals.
_CASE_SENSITIVE_MAKES = {"MG", "Seat", "Genesis", "Dodge", "Jaguar"}
# Makes and other names of makes that are also lowercase English words (a jeep, to ford a stream,
# a merc, alfa in the spelling alphabet): recognised only as written or in capitals, and then
# they count as a named make like any other.
_CAPITALISED_MAKES = {"Jeep", "Ford", "Merc", "Alfa"}

def _known_makes() -> list[str]:
    """English make names recognised in free text: the fleet catalog's makes and _OTHER_MAKES, longest first."""
    names = {m["make"] for m in kb.fleet_models()} | set(_OTHER_MAKES)
    return sorted(names, key=lambda name: (-len(name), name))


def _ascii_pattern(term: str) -> str:
    """A whole-word pattern for an English term; its words may be joined by spaces, dashes, dots, & or "and"."""
    tokens = re.findall(r"[A-Za-z0-9]+", term)
    body = r"(?:\s*(?:&|\+|and)\s*|[\s\-.]*)".join(re.escape(t) for t in tokens)
    return r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])"


def _he_pattern(term: str) -> str:
    """A Hebrew term at the start of a word, after at most one prefix letter, and ending a word."""
    return r"(?<![A-Za-zא-ת])[הבלומ]?" + re.escape(term) + r"(?![א-ת])"


def _after_skoda_pattern(model: str) -> str:
    """A Skoda model that is also an ordinary word: only right after Skoda, with spaces or a hyphen between."""
    return r"(?<![A-Za-z0-9])skoda[\s-]+" + re.escape(model) + r"(?![A-Za-z0-9])"


def _weak_pattern(name: str) -> str:
    """A make that is also an ordinary word, as written or in capitals; MG also with a number (MG4)."""
    if name == "MG":
        return r"(?<![A-Za-z0-9])MG(?![A-Za-z])"
    return r"(?<![A-Za-z0-9])(?:" + re.escape(name) + "|" + re.escape(name.upper()) + r")(?![A-Za-z0-9])"


def _normalize_make(make: str) -> Optional[str]:
    """Return canonical English make name or None if unrecognised."""
    if not make:
        return None
    for he, en in _HE_TO_MAKE.items():
        if he in make:
            return en
    make_lower = make.lower()
    for name in _known_makes():
        if name.lower() == make_lower:
            return name
    return None


def identify_vehicle(
    make: str,
    powertrain: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    Resolve (make, powertrain, model) to a platform cell.

    Only a Skoda with a petrol (or unstated) powertrain and a covered or unstated
    model resolves, to "skoda_gas"; everything else gets platform_cell None.
    """
    canonical = _normalize_make(make)
    if canonical is None:
        return {"platform_cell": None, "make": make, "reason": "unknown_make"}

    if canonical == "Skoda":
        if model is not None and model.lower() not in _SKODA_GAS_MODELS:
            return {"platform_cell": None, "make": "Skoda", "model": model, "reason": "non_gas_model"}
        if powertrain is None or powertrain.lower() in {"ice", "gas", "petrol"}:
            return {"platform_cell": "skoda_gas", "make": "Skoda", "powertrain": powertrain, "model": model}
        return {"platform_cell": None, "make": "Skoda", "reason": "non_gas_powertrain"}

    return {"platform_cell": None, "make": canonical, "reason": "not_skoda_gas"}


def _fold_caron(text: str) -> str:
    """Read "Skoda" written with a caron on the S (precomposed or combining) as Skoda."""
    return text.replace("\u0160", "S").replace("\u0161", "s").replace("\u030c", "")


def _word_in(word: str, lower: str) -> bool:
    """ASCII words match whole and hyphen-exact; Hebrew words match as substrings."""
    if word.isascii():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", lower) is not None
    return word in lower


def _powertrains(text: str) -> set[str]:
    """Every powertrain named in the text; a mild hybrid counts as petrol."""
    lower = text.lower()
    found: set[str] = set()
    if _IV_BADGE.search(text):
        found.add("phev")
    if _MILD_HYBRID.search(lower):
        found.add("petrol")
    lower = _MILD_HYBRID.sub(" ", lower)
    for word in _PETROL_WORDS:
        if _word_in(word, lower):
            found.add("petrol")
    for word, powertrain in _POWERTRAIN_WORDS:
        if _word_in(word, lower):
            found.add(powertrain)
    return found


def mentions(text: str) -> dict:
    """
    Every make, Skoda model and powertrain the text names.

    "makes" holds recognised makes (Hebrew spellings, English names and aliases; Jeep, Ford, Merc
    and Alfa only when capitalised), "weak_makes" the makes that are also ordinary English words
    (recognised only as written or in capitals), "models" the known Skoda models (Superb in lower case, and Rapid,
    only right after Skoda).
    """
    text = _fold_caron(text or "")
    makes: set[str] = set()
    weak: set[str] = set()
    models: set[str] = set()
    for he, make in _HE_TO_MAKE.items():
        if re.search(_he_pattern(he), text):
            makes.add(make)
    for name in _known_makes():
        if name in _CASE_SENSITIVE_MAKES:
            if re.search(_weak_pattern(name), text):
                weak.add(name)
        elif name in _CAPITALISED_MAKES:
            if re.search(_weak_pattern(name), text):
                makes.add(name)
        elif re.search(_ascii_pattern(name), text, re.IGNORECASE):
            makes.add(name)
    for alias, make in _EN_ALIASES.items():
        if alias in _CAPITALISED_MAKES:
            if re.search(_weak_pattern(alias), text):
                makes.add(make)
        elif re.search(_ascii_pattern(alias), text, re.IGNORECASE):
            makes.add(make)
    for name in _SKODA_GAS_MODEL_NAMES:
        if re.search(_ascii_pattern(name), text, re.IGNORECASE):
            models.add(name)
    for he, name in _SKODA_GAS_MODELS_HE.items():
        if re.search(_he_pattern(he), text):
            models.add(name)
    for spelling, name in _SKODA_OTHER_MODELS.items():
        if spelling.startswith("skoda "):
            pattern = _after_skoda_pattern(spelling.removeprefix("skoda "))
        elif spelling.isascii():
            pattern = _ascii_pattern(spelling)
        else:
            pattern = _he_pattern(spelling)
        if re.search(pattern, text, re.IGNORECASE):
            models.add(name)
    for name in _CAPITALISED_SKODA_MODELS:
        if re.search(_weak_pattern(name), text):
            models.add(name)
    return {"makes": makes, "weak_makes": weak, "models": models, "powertrains": _powertrains(text)}


def car_conflicts(m: dict, car: dict) -> bool:
    """
    True when the text names a recognised make other than the car's (a make that is also an ordinary
    word counts as written or in capitals) or, for a Skoda, a known Skoda model other than the car's.
    Powertrain words and unknown model names never conflict.
    """
    car_make = str(car["make"]).lower()
    if any(make.lower() != car_make for make in m["makes"] | m["weak_makes"]):
        return True
    if car_make == "skoda":
        return any(model.lower() != str(car["model"]).lower() for model in m["models"])
    return False


def vehicle_from_text(text: str) -> dict:
    """
    Identify the vehicle from a Skoda make, model and powertrain named in the text: {"platform_cell", "reason"}.

    Plates and numbers in the text are never read. Only "Skoda" with one covered model and a petrol
    powertrain resolves, and not when the word superb appears in any case; a recognised make other
    than Skoda, an uncovered Skoda model or a non-petrol Skoda gives non_supported_vehicle, and
    anything else vehicle_unknown.
    """
    text = _fold_caron(text or "")

    def result(cell: Optional[str], reason: Optional[str]) -> dict:
        return {"platform_cell": cell, "reason": reason}

    m = mentions(text)
    if (m["makes"] - {"Skoda"}) or m["weak_makes"]:
        return result(None, "non_supported_vehicle")
    if any(model.lower() not in _SKODA_GAS_MODELS for model in m["models"]):
        return result(None, "non_supported_vehicle")
    if (m["powertrains"] - {"petrol"}) and ("Skoda" in m["makes"] or m["models"]):
        return result(None, "non_supported_vehicle")
    if (m["makes"] == {"Skoda"} and len(m["models"]) == 1 and m["powertrains"] == {"petrol"}
            and not any(_word_in(name.lower(), text.lower()) for name in _CAPITALISED_SKODA_MODELS)):
        return result("skoda_gas", None)
    return result(None, "vehicle_unknown")


_FIELD_MAX = 40
_PLATE_FIELD = re.compile(r"([A-Z]{2})([0-9]{3})([0-9]{2})")
_PHONE_FIELD = re.compile(r"\+?[0-9 ().\-]*")


def field_given(value) -> bool:
    """True unless the field is missing, null, or blank."""
    return value is not None and not (isinstance(value, str) and not value.strip())


def normalise_plate(value) -> str | None:
    """The plate number field as FH-104-27; case, spaces and dashes do not matter. Anything else, a value that is not a string or one over 40 characters is not a plate."""
    if not isinstance(value, str) or len(value) > _FIELD_MAX:
        return None
    hit = _PLATE_FIELD.fullmatch(re.sub(r"[\s-]", "", value).upper())
    return f"{hit[1]}-{hit[2]}-{hit[3]}" if hit else None


def phone_digits(value) -> str | None:
    """The caller number as digits; spaces, dashes, dots, brackets and a leading + are ignored. Anything else is no number."""
    if not isinstance(value, str) or len(value) > _FIELD_MAX or not _PHONE_FIELD.fullmatch(value.strip()):
        return None
    return re.sub(r"[^0-9]", "", value) or None


def customer_by_phone(value) -> dict | None:
    """The customer on record with the same phone digits as the value; None when the value has none."""
    digits = phone_digits(value)
    if digits is None:
        return None
    return next((c for c in kb.customers() if phone_digits(c["phone"]) == digits), None)


def car_by_plate(value) -> tuple[dict, dict] | None:
    """(customer, vehicle) for the plate number on record; None for any other value."""
    plate = normalise_plate(value)
    if plate is None:
        return None
    return next(((c, v) for c in kb.customers() for v in c["vehicles"] if v["plate"] == plate), None)


def sheet_cells() -> list[str]:
    """Every platform cell an indicator record covers."""
    return sorted({cell for rec in kb.indicators().values() for cell in rec.get("vehicle_scope", [])})


def vehicle_for_request(text, caller_number=None, plate=None, customer_id=None, text_route=True) -> dict:
    """The vehicle of a request from the customer id, the caller number and the plate number field; see serve/README.md "Vehicle resolution". Returns {"platform_cell", "reason", "customer", "vehicle", "vehicles"}."""
    def result(cell=None, reason=None, customer=None, vehicle=None, vehicles=()) -> dict:
        return {"platform_cell": cell, "reason": reason, "customer": customer, "vehicle": vehicle,
                "vehicles": list(vehicles)}

    owner = None
    if field_given(customer_id):
        if isinstance(customer_id, str) and len(customer_id) <= _FIELD_MAX:
            owner = kb.customer(customer_id)
        if owner is None:
            return result(reason="unknown_customer")
        if field_given(caller_number):
            digits = phone_digits(caller_number)
            if digits is None or digits != phone_digits(owner["phone"]):
                return result(reason="customer_mismatch")
    elif field_given(caller_number):
        owner = customer_by_phone(caller_number)

    if field_given(plate):
        hit = car_by_plate(plate)
        if hit is None:
            return result(reason="plate_not_customers", customer=owner) if owner else result(reason="plate_unknown")
        plate_owner, car = hit
        if owner is not None and car["plate"] not in {v["plate"] for v in owner["vehicles"]}:
            return result(reason="plate_not_customers", customer=owner)
        owner = owner or plate_owner
    elif owner is not None:
        if len(owner["vehicles"]) != 1:
            return result(reason="choose_vehicle", customer=owner, vehicles=owner["vehicles"])
        car = owner["vehicles"][0]
    else:
        reason = "unknown_caller" if field_given(caller_number) else "vehicle_unknown"
        if not text_route:
            return result(reason=reason)
        found = vehicle_from_text(text)
        if found["platform_cell"] is None and found["reason"] == "vehicle_unknown":
            return result(reason=reason)
        return result(found["platform_cell"], found["reason"])

    if car_conflicts(mentions(text or ""), car):
        return result(reason="vehicle_mismatch", customer=owner, vehicle=car)
    cell = identify_vehicle(car["make"], powertrain=car["powertrain"], model=car["model"])["platform_cell"]
    return result(cell, None if cell else "non_supported_vehicle", owner, car)


def lookup_indicator(indicator_id: str, platform_cell: str) -> Optional[dict]:
    """
    Look up an indicator record by bare or qualified id.

    Returns None if the record is missing or its vehicle_scope excludes platform_cell.
    """
    if "." not in indicator_id:
        qualified = f"{platform_cell}.{indicator_id}"
    else:
        qualified = indicator_id

    rec = kb.indicator(qualified)
    if rec is None:
        return None

    if platform_cell not in rec.get("vehicle_scope", []):
        return None

    return rec


def get_fleet_procedure(procedure_id: str) -> Optional[dict]:
    """Return the fleet rulebook entry for procedure_id, unchanged."""
    return kb.procedure(procedure_id)


def drivability_assessment(indicator_rec: dict) -> dict:
    """
    Drivability verdict derived only from the record's severity and hard_stop.

    hard_stop wins over severity, and an unknown or missing severity is treated as
    not drivable.
    """
    severity = indicator_rec.get("severity")
    hard_stop = indicator_rec.get("hard_stop", False)
    message_text = indicator_rec.get("message_text")

    if hard_stop is True or severity == "stop_now":
        return {
            "status": "not_drivable",
            "verdict": "stop_now",
            "severity": severity,
            "hard_stop": hard_stop,
            "message_text": message_text,
        }

    if severity == "urgent":
        return {
            "status": "drivable_caution",
            "verdict": "conditional",
            "severity": severity,
            "hard_stop": False,
            "conditional": True,
            "message_text": message_text,
        }

    if severity == "caution":
        return {
            "status": "drivable_caution",
            "verdict": "caution",
            "severity": severity,
            "hard_stop": False,
            "message_text": message_text,
        }

    if severity == "info":
        return {
            "status": "drivable",
            "verdict": "info",
            "severity": severity,
            "hard_stop": False,
            "message_text": message_text,
        }

    return {
        "status": "not_drivable",
        "verdict": "escalate",
        "severity": severity,
        "hard_stop": False,
        "message_text": None,
    }


def fired_context_rules(bare_id: str, context_flags) -> list[dict]:
    """Context rules whose flags are all in context_flags and whose scope includes bare_id."""
    flags = set(context_flags)
    return [
        rule for rule in kb.context_rules()
        if rule["when_flags"]
        and flags.issuperset(rule["when_flags"])
        and rule.get("when_symptom") in (None, "", bare_id)
    ]


def apply_context(assessment: dict, bare_id: str, context_flags) -> dict:
    """
    Tighten a verdict with the context rules that fire for this request.

    Context can only make the result more cautious: a drivable verdict may become tow
    or escalate (tow wins if both fire), and a verdict that is already not drivable is
    returned unchanged. raise_conservatism rules do not change a single verdict; the
    orchestrator applies them to ambiguous queries. Severity is never changed.
    """
    if assessment["status"] == "not_drivable":
        return assessment
    fired = [
        rule for rule in fired_context_rules(bare_id, context_flags)
        if rule["then"] in ("tow", "escalate")
    ]
    if not fired:
        return assessment
    verdict = "tow" if any(rule["then"] == "tow" for rule in fired) else "escalate"
    return {
        **assessment,
        "status": "not_drivable",
        "verdict": verdict,
        "context_flags": sorted({f for rule in fired for f in rule["when_flags"]}),
    }


# Planned tools, not implemented yet.

def triage_checklist(indicator_id: str) -> dict:
    """Structured triage checklist per indicator. Not implemented."""
    raise NotImplementedError(
        "triage_checklist is deferred to a future phase; "
        "implement as a structured per-indicator checklist from the manual."
    )


def render_form(form_type: str, context: dict) -> dict:
    """Render a structured sign-off or operator form. Not implemented."""
    raise NotImplementedError(
        "render_form is deferred to a future phase; "
        "implement form templating over the knowledge schema."
    )


def drivability_gate(indicator_ids: list, context: dict) -> dict:
    """Multi-indicator drivability gate. Not implemented."""
    raise NotImplementedError(
        "drivability_gate is deferred to a future phase; "
        "implement as a rule engine combining multiple indicator assessments."
    )
