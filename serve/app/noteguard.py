"""
A word and character filter on model text shown to the operator. Text that contains a listed verdict,
drivability, severity or dispatch stem (English, Hebrew or Arabic), a rendered verdict label, or a character
outside ASCII, Hebrew and Arabic is withheld. It matches words; it does not judge meaning.
"""
import unicodedata

from app.compose import verdict_label

NOTE_MAX_CHARS = 2000
# A model note containing any of these stems (normalised, lowercased, whitespace collapsed) is
# withheld: verdict, drivability and severity wording first, then dispatch wording.
WITHHOLD_STEMS = (
    "driv", "safe", "sever", "stop", "tow", "operational", "verdict", "caution", "danger",
    "ok to", "fine to", "roadworthy", "continue",
    "dispatch", "rescue", "recovery", "dealer", "appointment", "truck", "roadside", "booked",
    "pull over", "carry on",
)
# The same wording in Hebrew (the compose.py labels and their relatives) and in Arabic.
HEBREW_STEMS = (
    "לנסוע", "נסיעה", "תקין",
    "בטוח", "זהירות", "גרירה",
    "גרור", "גרר", "נהיג", "לנהוג",
    "חומרה", "עצור", "עצירה",
    "סכנה", "להמשיך", "כשיר",
    "חילוץ", "מוסך",
)
ARABIC_STEMS = (
    "\u0642\u064a\u0627\u062f", "\u0642\u0648\u062f", "\u0622\u0645\u0646", "\u0623\u0645\u0646",
    "\u0627\u0645\u0646", "\u0623\u0645\u0627\u0646", "\u0627\u0645\u0627\u0646", "\u0633\u062d\u0628",
    "\u0648\u0646\u0634", "\u0633\u0637\u062d\u0629", "\u0642\u0637\u0631", "\u062a\u0648\u0642\u0641",
    "\u062e\u0637\u0631", "\u062e\u0637\u064a\u0631", "\u062a\u062d\u0630\u064a\u0631", "\u062d\u0630\u0631",
    "\u0635\u0627\u0644\u062d", "\u0633\u0644\u064a\u0645", "\u062c\u0627\u0647\u0632", "\u062a\u0639\u0645\u0644",
    "\u0648\u0627\u0635\u0644", "\u062a\u0627\u0628\u0639", "\u0627\u0633\u062a\u0645\u0631",
    "\u0625\u0646\u0642\u0627\u0630", "\u0645\u0648\u0639\u062f", "\u0648\u0643\u064a\u0644",
    "\u0634\u0627\u062d\u0646\u0629",
)
ALL_STEMS = WITHHOLD_STEMS + HEBREW_STEMS + ARABIC_STEMS
VERDICTS = ("stop_now", "conditional", "caution", "info", "tow", "escalate")
LABEL_LANGUAGES = ("he-IL", "en-US", "ar-SA", "ru-RU")
HEB_ARABIC_FIRST, HEB_ARABIC_LAST = 0x590, 0x6FF
WITHHELD_NOTE = ("The model's note was withheld because it touched on the verdict, drivability or dispatch. "
                 "The carmind verdict below is the one that counts.")


def _clean(text) -> str:
    """NFKC, no format characters (zero-width, soft hyphen, ...), whitespace collapsed to single spaces."""
    text = unicodedata.normalize("NFKC", text if isinstance(text, str) else "")
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return " ".join(text.split())


def _match_form(clean: str) -> str:
    """Lowercase, without the vowel and reading marks of Hebrew and Arabic, for stem matching."""
    lowered = clean.lower()
    return "".join(c for c in lowered
                   if not (HEB_ARABIC_FIRST <= ord(c) <= HEB_ARABIC_LAST and unicodedata.category(c) == "Mn"))


def _label_form(label: str) -> str:
    """A compose.py verdict label in match form, without its closing full stop."""
    return _match_form(_clean(label)).rstrip(". ")


# Every verdict label compose.py renders, in every language, in match form.
VERDICT_LABEL_FORMS = tuple(sorted({_label_form(verdict_label(v, lang))
                                    for v in VERDICTS for lang in LABEL_LANGUAGES}))


def guard_note(text: str) -> str:
    """The model's text on one line, or WITHHELD_NOTE when it touches on the verdict or dispatch."""
    flat = _clean(text)[:NOTE_MAX_CHARS]
    if any(ord(c) > 127 and not HEB_ARABIC_FIRST <= ord(c) <= HEB_ARABIC_LAST for c in flat):
        return WITHHELD_NOTE
    matchable = _match_form(flat)
    if any(stem in matchable for stem in ALL_STEMS) or any(form in matchable for form in VERDICT_LABEL_FORMS):
        return WITHHELD_NOTE
    return flat
