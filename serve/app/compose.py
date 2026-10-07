"""
Layered reply composer.

Section A is the fleet's own rulebook entry, shown first, verbatim and attributed.
Section B is the carmind assessment, with severity and hard_stop taken unchanged from
the indicator record. No reply ever says "safe to drive" / "בטוח לנסיעה".
"""
from app import tools
from app.match import INDICATOR_TO_PROCEDURE


def _is_he(language: str) -> bool:
    return language.startswith("he")


def _labels(language: str) -> dict:
    if _is_he(language):
        return {
            "sec_a": "מדריך הצי (חילוץ):",
            "attrib": "מקור: נוהל הצי",
            "sec_b": "הערכת carmind:",
            "severity_lbl": "חומרה:",
            "source_lbl": "מקור יצרן:",
            "page_sep": "עמ'",
            "do_not_drive": "אין לנסוע.",
            "conditional": "נסיעה מותנית - בדוק תחילה.",
            "caution": "נסיעה בזהירות.",
            "operational": "הרכב תקין לנסיעה.",
            "escalate_verdict": "יש לפנות לגורם מוסמך.",
            "tow": "יש לגרור את הרכב - אין לנסוע.",
            "context_lbl": "הקשר:",
            "stop_precaution": "אין לנסוע עד שהרכב ייבדק.",
        }
    # en-US, ar-SA and ru-RU get English labels; the rulebook body stays in Hebrew.
    return {
        "sec_a": "Fleet recovery guide:",
        "attrib": "Source: fleet rulebook",
        "sec_b": "carmind assessment:",
        "severity_lbl": "Severity:",
        "source_lbl": "Manufacturer source:",
        "page_sep": "p.",
        "do_not_drive": "Do not drive.",
        "conditional": "Conditional - check first before driving.",
        "caution": "Drive with caution.",
        "operational": "Vehicle operational.",
        "escalate_verdict": "Contact a qualified specialist.",
        "tow": "Tow recommended - do not drive.",
        "context_lbl": "Context:",
        "stop_precaution": "Do not drive until the car has been checked.",
    }


def stop_precaution(language: str) -> str:
    """The do-not-drive line added to a reply without an assessment when the light means stop."""
    return _labels(language)["stop_precaution"]


def verdict_label(verdict: str | None, language: str) -> str:
    """The verdict line for an assessment verdict, in the reply language."""
    lbl = _labels(language)
    if verdict == "stop_now":
        return lbl["do_not_drive"]
    elif verdict == "conditional":
        return lbl["conditional"]
    elif verdict == "caution":
        return lbl["caution"]
    elif verdict == "info":
        return lbl["operational"]
    elif verdict == "tow":
        return lbl["tow"]
    return lbl["escalate_verdict"]


def verdict_text(indicator_rec: dict, assessment: dict) -> str:
    """One-paragraph English verdict for the agent route, from the record and the deterministic assessment."""
    lbl = _labels("en-US")
    name = indicator_rec.get("name_en") or indicator_rec.get("std_name") or indicator_rec.get("id", "")
    parts = [f"{name}. {lbl['severity_lbl']} {assessment.get('severity', 'unknown')}.",
             verdict_label(assessment.get("verdict"), "en-US")]
    if assessment.get("verdict") in ("stop_now", "conditional") and assessment.get("message_text"):
        parts.append(f"Manual: {assessment['message_text']}")
    if assessment.get("context_flags"):
        parts.append(f"{lbl['context_lbl']} {', '.join(assessment['context_flags'])}")
    return " ".join(parts)


def compose_reply(
    indicator_rec: dict,
    assessment: dict,
    fleet_proc: dict | None,
    language: str,
) -> str:
    """Compose the full layered reply for a single identified indicator."""
    lbl = _labels(language)
    parts: list[str] = []

    # Section A: fleet rulebook entry
    parts.append(lbl["sec_a"])
    if fleet_proc:
        name_he = fleet_proc.get("light_name_he", "")
        explanation_he = fleet_proc.get("explanation_he", "")
        recovery_he = fleet_proc.get("recovery_action_he", "")
        parts.append(f"{name_he} - {explanation_he}")
        parts.append(recovery_he)
    parts.append(lbl["attrib"])
    parts.append("")

    # Section B: carmind assessment
    parts.append(lbl["sec_b"])

    name_en = indicator_rec.get("name_en", indicator_rec.get("std_name", ""))
    meaning_en = indicator_rec.get("meaning_en", "")
    parts.append(f"{name_en}: {meaning_en}")

    severity = assessment.get("severity", "unknown")
    parts.append(f"{lbl['severity_lbl']} {severity}")

    verdict = assessment.get("verdict")
    parts.append(verdict_label(verdict, language))

    # Set only when a context rule tightened the verdict.
    if assessment.get("context_flags"):
        parts.append(f"{lbl['context_lbl']} {', '.join(assessment['context_flags'])}")

    prov = indicator_rec.get("provenance", {})
    manual_id = prov.get("manual_id", "")
    source_page = prov.get("source_page", "")
    if manual_id and source_page:
        parts.append(f"{lbl['source_lbl']} {manual_id} {lbl['page_sep']}{source_page}")

    # stop_now repeats the manual's own message and ends on an explicit do-not-drive line.
    if verdict == "stop_now":
        msg = assessment.get("message_text")
        if msg:
            parts.append(msg)
        parts.append(lbl["do_not_drive"])

    elif verdict == "conditional":
        msg = assessment.get("message_text")
        if msg:
            parts.append(msg)

    return "\n".join(parts)


def light_name(rec: dict, language: str) -> str:
    if _is_he(language):
        bare = (rec.get("id") or "").split(".")[-1]
        proc_id = INDICATOR_TO_PROCEDURE.get(bare)
        proc = tools.get_fleet_procedure(proc_id) if proc_id else None
        name_he = (proc.get("light_name_he") if proc else None) \
            or rec.get("std_name") or rec.get("id", "")
        return name_he
    return rec.get("name_en") or rec.get("std_name") or rec.get("id", "")


def compose_ambiguous(
    candidate_records: list[dict],
    language: str,
    force_precaution: bool = False,
) -> str:
    """
    Reply for more than one surviving candidate: list the light names and ask which one.

    Gives no verdict. If any candidate is stop_now or hard_stop, or force_precaution is set
    (a context rule fired, a candidate is not drivable on its own record, or a light is
    described in a state its record does not cover), adds a do-not-drive precaution.
    """
    is_he = _is_he(language)

    names: list[str] = []
    has_stop_level = force_precaution

    for rec in candidate_records:
        severity = rec.get("severity")
        hard_stop = rec.get("hard_stop", False)
        if severity == "stop_now" or hard_stop is True:
            has_stop_level = True
        names.append(light_name(rec, language))

    joined = ", ".join(names)

    if is_he:
        reply = (
            f"זוהו מספר נורות אפשריות: {joined}.\n"
            "אנא פרט איזו נורה בדיוק דולקת כדי שנוכל לסייע."
        )
        if has_stop_level:
            reply += "\n\nאזהרה: ייתכן ומדובר בתקלה חמורה - אין לנסוע עד לבירור."
    else:
        reply = (
            f"Multiple possible warning lights identified: {joined}.\n"
            "Please specify which light exactly is illuminated so we can assist."
        )
        if has_stop_level:
            reply += "\n\nCaution: a serious fault is possible - do not drive until the issue is clarified."

    return reply


def compose_escalate(language: str, reason: str = "no_match", vehicles=(), text_route: bool = True) -> str:
    """Conservative escalate reply for no match, unsupported vehicle, unidentified vehicle
    (the request for the plate number), unknown caller or plate, a customer with several cars, and
    caller, customer or plate mismatches, or error."""
    is_he = _is_he(language)
    cars = "; ".join(f"{v['plate']} ({v['year']} {v['make']} {v['model']}, {v['powertrain']})" for v in vehicles)

    _msgs: dict[str, dict[str, str]] = {
        "no_match": {
            "he": "לא הצלחנו לזהות את הנורה שתיארת. יש לפנות לגורם מוסמך לבדיקה.",
            "en": "We could not identify the dashboard light from the description provided. Please contact a qualified specialist.",
        },
        "non_supported_vehicle": {
            "he": "לא נמצא מידע עבור כלי הרכב שצוין. יש לפנות לגורם מוסמך לבדיקה.",
            "en": "No information found for the specified vehicle. Please contact a qualified specialist.",
        },
        "vehicle_unknown": {
            "he": "carmind צריך לדעת מהו הרכב: יש להזין את מספר הרישוי בשדה מספר הרישוי. ללא מספר רישוי או מספר טלפון של המתקשר שנמצאים ברשומות, או יצרן, דגם וסוג הנעה של סקודה שצוינו בתיאור, לא תינתן הערכה. אם מספר הרישוי אינו זמין, יש לפנות לגורם מוסמך לבדיקה.",
            "en": "carmind needs the vehicle: enter its plate number in the plate number field. Without a plate number or caller number on record, or a Skoda make, model and powertrain named in the description, there is no assessment. If the plate number is not available, please contact a qualified specialist.",
        } if text_route else {
            "he": "carmind צריך לדעת מהו הרכב: יש להזין את מספר הרישוי בשדה מספר הרישוי. ללא מזהה לקוח, מספר טלפון של המתקשר או מספר רישוי שנמצאים ברשומות, לא תינתן הערכה. אם מספר הרישוי אינו זמין, יש לפנות לגורם מוסמך לבדיקה.",
            "en": "carmind needs the vehicle: enter its plate number in the plate number field. Without a customer id, caller number or plate number on record there is no assessment. If the plate number is not available, please contact a qualified specialist.",
        },
        "unknown_caller": {
            "he": "מספר הטלפון של המתקשר אינו ברשומות. יש להזין את מספר הרישוי בשדה מספר הרישוי, או לציין בתיאור יצרן, דגם וסוג הנעה של סקודה; בלי אחד מהם לא תינתן הערכה. אם מספר הרישוי אינו זמין, יש לפנות לגורם מוסמך לבדיקה.",
            "en": "The caller number is not on record. Enter the plate number in the plate number field, or name the Skoda make, model and powertrain in the description; without one of them there is no assessment. If the plate number is not available, please contact a qualified specialist.",
        } if text_route else {
            "he": "מספר הטלפון של המתקשר אינו ברשומות: יש להזין את מספר הרישוי בשדה מספר הרישוי. בלעדיו לא תינתן הערכה. אם מספר הרישוי אינו זמין, יש לפנות לגורם מוסמך לבדיקה.",
            "en": "The caller number is not on record: enter the plate number in the plate number field. Without it there is no assessment. If the plate number is not available, please contact a qualified specialist.",
        },
        "plate_unknown": {
            "he": "מספר הרישוי אינו ברשומות, ולכן לא תינתן הערכה. יש לבדוק את מספר הרישוי או לפנות לגורם מוסמך לבדיקה.",
            "en": "The plate number is not on record, so there is no assessment. Please check the plate number or contact a qualified specialist.",
        },
        "choose_vehicle": {
            "he": f"ללקוח רשומים יותר מרכב אחד: {cars}. יש להזין את מספר הרישוי של הרכב בשדה מספר הרישוי; לא תינתן הערכה עד שהרכב ידוע.",
            "en": f"This customer has more than one car on record: {cars}. Enter the plate number of the car in the plate number field; there is no assessment until the car is known.",
        },
        "plate_not_customers": {
            "he": "מספר הרישוי אינו אחד מכלי הרכב הרשומים על שם הלקוח, ולכן לא תינתן הערכה. יש לבדוק את מספר הרישוי או לפנות לגורם מוסמך לבדיקה.",
            "en": "The plate number is not one of this customer's cars on record, so there is no assessment. Please check the plate number or contact a qualified specialist.",
        },
        "customer_mismatch": {
            "he": "מספר הטלפון של המתקשר אינו של הלקוח שנבחר, ולכן לא תינתן הערכה. יש לבדוק את מספר הטלפון או לפנות לגורם מוסמך לבדיקה.",
            "en": "The caller number does not belong to the selected customer, so there is no assessment. Please check the caller number or contact a qualified specialist.",
        },
        "vehicle_mismatch": {
            "he": "הרכב שברשומות והרכב שתואר אינם תואמים, ולכן לא תינתן הערכה. יש לבדוק את מספר הרישוי או לפנות לגורם מוסמך לבדיקה.",
            "en": "The car on record and the vehicle described do not match, so there is no assessment. Please check the plate number or contact a qualified specialist.",
        },
        "error": {
            "he": "אירעה שגיאה בעיבוד הבקשה. יש לפנות לגורם מוסמך לבדיקה.",
            "en": "An error occurred while processing the request. Please contact a qualified specialist.",
        },
    }

    bucket = _msgs.get(reason, _msgs["no_match"])
    return bucket["he"] if is_he else bucket["en"]


def build_indicator_reply(
    bare_id: str,
    platform_cell: str,
    language: str,
    context_flags=(),
) -> str:
    """Look up bare_id, assess it, apply context, attach the rulebook entry and compose."""
    rec = tools.lookup_indicator(bare_id, platform_cell)
    if rec is None:
        return compose_escalate(language, "no_match")

    assessment = tools.apply_context(tools.drivability_assessment(rec), bare_id, context_flags)

    proc_id = INDICATOR_TO_PROCEDURE.get(bare_id)
    fleet_proc = tools.get_fleet_procedure(proc_id) if proc_id else None

    return compose_reply(rec, assessment, fleet_proc, language)


CONTRACT_CHOICE_TEXT = {
    "en": {"picked": ("Light picked by a language model from the matched lights ({names}): {picked}. Words in the "
                      "description that match it: {words}. The verdict is that light's own record."),
           "asked": "A language model could not pick one of the matched lights ({names}), so the caller is asked."},
    "he": {"picked": ("נורה שמודל שפה בחר מבין הנורות שזוהו ({names}): {picked}. מילים בתיאור שמתאימות לה: {words}. "
                      "ההערכה היא של הרשומה של נורה זו."),
           "asked": "מודל שפה לא בחר אחת מהנורות שזוהו ({names}), ולכן יש לשאול את הפונה."},
}
AGENT_CHOICE_TEXT = {
    "picked": ("Light picked by a language model from the matched lights ({names}): {picked}. The verdict is that "
               "light's own record; the dispatch is the more cautious of the plan for this light and the plan for an "
               "unconfirmed light. Model reasoning (not a verdict): {reasoning}"),
    "asked": ("A language model could not pick one of the matched lights ({names}). Model reasoning (not a verdict): "
              "{reasoning}"),
    "withheld": "withheld by carmind's word and character filter.",
    "none": "none given.",
}
CONTRACT_FLAGS_TEXT = {
    "en": ("Context inferred by a language model from the description: {flags}. Context can only make the verdict "
           "more cautious."),
    "he": "הקשר שמודל שפה הסיק מהתיאור: {flags}. הקשר יכול רק להחמיר את ההערכה.",
}
AGENT_FLAGS_TEXT = ("Context inferred by a language model from the description: {flags}. Context can only make the "
                    "verdict more cautious. Model reasoning (not a verdict): {reasoning}")


def _name_list(names) -> str:
    return ", ".join(dict.fromkeys(names))


def contract_choice_line(status: str, picked_name, names, words, language: str) -> str:
    """The POST / paragraph about a light choice, written by code; "" unless picked or asked."""
    texts = CONTRACT_CHOICE_TEXT["he" if _is_he(language) else "en"]
    if status not in texts:
        return ""
    return texts[status].format(names=_name_list(names), picked=picked_name or "", words=", ".join(words))


def agent_choice_line(status: str, picked_name, names, reasoning: str, withheld: bool) -> str:
    if status not in ("picked", "asked"):
        return ""
    shown = AGENT_CHOICE_TEXT["withheld"] if withheld else (reasoning or AGENT_CHOICE_TEXT["none"])
    return AGENT_CHOICE_TEXT[status].format(names=_name_list(names), picked=picked_name or "", reasoning=shown)


def contract_flags_line(flags, language: str) -> str:
    """The POST / paragraph naming inferred context flags, written by code; "" when there are none."""
    return CONTRACT_FLAGS_TEXT["he" if _is_he(language) else "en"].format(flags=", ".join(flags)) if flags else ""


def agent_flags_line(flags, reasoning: str, withheld: bool) -> str:
    if not flags:
        return ""
    shown = AGENT_CHOICE_TEXT["withheld"] if withheld else (reasoning or AGENT_CHOICE_TEXT["none"])
    return AGENT_FLAGS_TEXT.format(flags=", ".join(flags), reasoning=shown)
