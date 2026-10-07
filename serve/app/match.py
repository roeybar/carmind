"""
Deterministic keyword matcher: free text to candidate bare indicator ids.

Ids are qualified with the platform cell later, in lookup_indicator.
"""
import re

# (bare_id, en_keywords, he_keywords).
# Latin keywords must match whole words: a substring match let "esc" hit "describe"
# and answer an unrelated question with the ESC "info" verdict.
# Hebrew keywords match as substrings, since Hebrew attaches prefixes to words.
_TABLE: list[tuple[str, list[str], list[str]]] = [
    (
        "brake_fluid_low",
        ["brake fluid", "red brake", "brake warning", "brake system"],
        ["נוזל בלמים", "בלם", "בלמים"],
    ),
    (
        "brake_abs_fault",
        ["abs", "anti-lock", "anti lock"],
        ["abs"],
    ),
    (
        "coolant_level_low",
        ["coolant", "cooling"],
        ["נוזל קירור", "קירור"],
    ),
    (
        "engine_control_petrol",
        ["check engine", "engine control", "epc", "engine light", "petrol engine"],
        ["מנוע", "epc"],
    ),
    (
        "front_assist_collision_warning",
        ["front assist", "collision", "forward collision"],
        ["התנגשות", "מרחק"],
    ),
    (
        "esc_active",
        ["esc", "stability", "traction"],
        ["יציבות", "החלקה"],
    ),
]

# Fleet rulebook procedure shown alongside each indicator.
INDICATOR_TO_PROCEDURE: dict[str, str] = {
    "brake_fluid_low": "brake-fluid",
    "brake_abs_fault": "brake-abs",
    "coolant_level_low": "coolant-level",
    "engine_control_petrol": "engine-epc",
    "front_assist_collision_warning": "driver-assist",
    "esc_active": "driver-assist",
}

# esc_active describes a flashing ESC light: its verdict needs the text to name that state,
# and any other description (on, lit, stays on, not flashing) is a state the record does not
# cover. coolant_level_low describes a low level, not overheating.
_ESC_FLASHING_EN = re.compile(r"\b(?:flash|blink)\w*")
_ESC_FLASHING_HE = ["הבהב", "הבהוב"]
_ESC_NOT_FLASHING_EN = re.compile(
    r"(?:\bnot|n['\u2019]t|\bnever|\bno longer|\bstopped)\s+(?:flash|blink)"
    r"|\b(?:stays?|stayed|staying|remains?|remained) (?:on|lit)\b"
    r"|\b(?:solid|steady|steadily)\b"
    r"|\bon all the time\b"
    r"|(?:\bnot|n['\u2019]t) go(?:ing)? off\b"
)
_ESC_NOT_FLASHING_HE = ["לא מהבהב", "לא הבהב", "ללא הבהוב", "בלי הבהוב", "הפסיק להבהב",
                        "הפסיקה להבהב", "נשאר", "דולק קבוע", "דולקת קבוע",
                        "דולק באופן קבוע", "דולקת באופן קבוע"]
_COOLANT_OVERHEAT_EN = re.compile(
    r"\b(?:over-?heat\w*|temperatures?|temps?|boil\w*|steam\w*)\b"
    r"|\b(?:too|running|runs|ran) hot\b"
)
_COOLANT_OVERHEAT_HE = ["טמפרטור", "התחממ", "התחמם", "מתחמם", "מתחממ", "חם מדי", "רותח", "רתיח"]


def _in_excluded_state(bare_id: str, lower: str) -> bool:
    """True if lower describes bare_id in a state its record does not cover."""
    if bare_id == "esc_active":
        flashing = _ESC_FLASHING_EN.search(lower) or any(k in lower for k in _ESC_FLASHING_HE)
        not_flashing = _ESC_NOT_FLASHING_EN.search(lower) or any(k in lower for k in _ESC_NOT_FLASHING_HE)
        return not flashing or bool(not_flashing)
    if bare_id == "coolant_level_low":
        return bool(_COOLANT_OVERHEAT_EN.search(lower)) or any(k in lower for k in _COOLANT_OVERHEAT_HE)
    return False


def _kw_matches(kw: str, lower_text: str) -> bool:
    if kw.isascii():
        return re.search(r'\b' + re.escape(kw) + r'\b', lower_text) is not None
    return kw in lower_text


def _keyword_hits(lower: str) -> set[str]:
    """Bare ids whose keywords appear in lower, after the brake/ABS disambiguation."""
    matched: set[str] = set()

    for bare_id, en_kws, he_kws in _TABLE:
        hit = any(_kw_matches(kw, lower) for kw in en_kws)
        if not hit:
            hit = any(_kw_matches(kw, lower) for kw in he_kws)
        if hit:
            matched.add(bare_id)

    if "brake_fluid_low" in matched and "brake_abs_fault" in matched:
        has_abs = any(_kw_matches(k, lower) for k in ("abs", "anti-lock", "anti lock"))
        if has_abs:
            matched.discard("brake_fluid_low")
        else:
            matched.discard("brake_abs_fault")

    return matched


def match(text: str) -> list[str]:
    """
    Return the sorted bare indicator ids whose keywords appear in text.

    If both brake indicators hit, an ABS token keeps brake_abs_fault, otherwise
    brake_fluid_low is kept. More than one survivor is handled by the orchestrator
    as an ambiguous query. An id described in a state its record does not cover is left out (see excluded).
    """
    lower = text.lower()
    return sorted(i for i in _keyword_hits(lower) if not _in_excluded_state(i, lower))


def excluded(text: str) -> list[str]:
    """Sorted bare ids whose keywords appear in text, but in a state their record does not cover."""
    lower = text.lower()
    return sorted(i for i in _keyword_hits(lower) if _in_excluded_state(i, lower))


def keywords_in(bare_id: str, text: str) -> list[str]:
    """The keywords of bare_id (English, then Hebrew) that appear in text."""
    lower = text.lower()
    return list(dict.fromkeys(kw for row in _TABLE if row[0] == bare_id for kw in (*row[1], *row[2])
                              if _kw_matches(kw, lower)))
