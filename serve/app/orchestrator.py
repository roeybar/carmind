"""
Request pipeline: identify the vehicle, match indicators, look them up, compose.

The drivability text always comes from the deterministic tools and composer. With
LUNA_MODE=on a language model may add context flags, which only tighten, and may pick
one of several matched lights that are all drivable on their own record (see luna.py).
A light described in a state its record does not cover gets no verdict for any light.
"""
import functools
import heapq
import logging
import re
import unicodedata

from app import kb, luna
from app.compose import (
    compose_ambiguous,
    compose_escalate,
    compose_reply,
    contract_choice_line,
    contract_flags_line,
    light_name,
    stop_precaution,
)
from app.match import _TABLE, INDICATOR_TO_PROCEDURE, excluded, keywords_in, match
from app.models import ChatRequest
from app.tools import (
    apply_context,
    drivability_assessment,
    fired_context_rules,
    get_fleet_procedure,
    lookup_indicator,
    sheet_cells,
    vehicle_for_request,
)

logger = logging.getLogger(__name__)

_SNIPPET_WINDOW = 40
_SNIPPET_MAX = 200

# Plate-shaped tokens are masked out of the snippet sent to the model; they are never read as a plate.
# A separator may be whitespace, ".", "_" or any dash (Unicode Pd).
_PLATE_SEP = r"[\s._\-\u058a\u05be\u1400\u1806\u2010-\u2015\u2e17\u2e1a\u2e3a\u2e3b\u2e40\u2e5d\u301c\u3030\u30a0\ufe31\ufe32\ufe58\ufe63\uff0d\U00010d6e\U00010ead]"
_PLATE_SHAPE = re.compile(r"[A-Za-z]{2}" + _PLATE_SEP + r"?[0-9]{3}" + _PLATE_SEP + r"?[0-9]{2}")
_TOKEN_SCAN_MAX = 254
_SECRET_CHAR = re.compile(r"[\d@]")
_TOKEN = re.compile(r"\S+")


@functools.lru_cache(maxsize=4096)
def _secret_char(ch: str) -> bool:
    return bool(_SECRET_CHAR.search(unicodedata.normalize("NFKC", ch)))


def _is_secret(token: str) -> bool:
    """True when the token holds a digit or @, also in a form NFKC folds to one. Each character is checked on its
    own, which gives the same answer as normalizing the whole token and keeps the cost linear."""
    if _SECRET_CHAR.search(token):
        return True
    if token.isascii():
        return False
    return any(_secret_char(ch) for ch in token)


def _token_span(text: str, i: int) -> tuple[int, int, bool]:
    """The whitespace-delimited token around position i, read at most _TOKEN_SCAN_MAX characters each way, and
    whether both ends were reached."""
    s = i
    while s > 0 and not text[s - 1].isspace() and i - s < _TOKEN_SCAN_MAX:
        s -= 1
    e = i
    while e < len(text) and not text[e].isspace() and e - i < _TOKEN_SCAN_MAX:
        e += 1
    whole = (s == 0 or text[s - 1].isspace()) and (e == len(text) or text[e].isspace())
    return s, e, whole


def _window(text: str, start: int, end: int) -> str:
    """text[start:end]; a token cut by either edge becomes [REDACTED] when the whole token is secret."""
    piece = text[start:end]
    if 0 < start < len(text) and not text[start - 1].isspace() and not text[start].isspace():
        s, e, whole = _token_span(text, start)
        if not whole or _is_secret(text[s:e]):
            k = 0
            while k < len(piece) and not piece[k].isspace():
                k += 1
            piece = "[REDACTED]" + piece[k:]
    if 0 < end < len(text) and not text[end - 1].isspace() and not text[end].isspace():
        s, e, whole = _token_span(text, end)
        if not whole or _is_secret(text[s:e]):
            k = len(piece)
            while k > 0 and not piece[k - 1].isspace():
                k -= 1
            piece = piece[:k] + "[REDACTED]"
    return piece


def _scrub(text: str) -> str:
    return _TOKEN.sub(lambda m: "[REDACTED]" if _is_secret(m.group()) else m.group(), text)


def _redact_spans(text: str) -> str:
    """Plate shapes and every token holding a digit or @, both found on the original text, replaced as one
    union of spans: a plate written with a separator is masked whole, and no fragment of a secret token remains."""
    plates = (m.span() for m in _PLATE_SHAPE.finditer(text))
    secrets = (m.span() for m in _TOKEN.finditer(text) if _is_secret(m.group()))
    out = []
    pos = 0
    cur_s = cur_e = -1
    for s, e in heapq.merge(plates, secrets):
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
            continue
        if cur_e >= 0:
            out.append(text[pos:cur_s])
            out.append("[REDACTED]")
            pos = cur_e
        cur_s, cur_e = s, e
    if cur_e >= 0:
        out.append(text[pos:cur_s])
        out.append("[REDACTED]")
        pos = cur_e
    out.append(text[pos:])
    return "".join(out)


def _build_snippet(text: str, hit_keywords: list[str], window: int = _SNIPPET_WINDOW,
                   limit: int = _SNIPPET_MAX) -> str:
    """Keyword windows for luna: plate shapes and every token holding a digit or @ are masked first, as one
    union of spans found on the original text; then PII-masked and scrubbed again, cut to limit."""
    text = _redact_spans(text)
    ranges: list[tuple[int, int]] = []
    for kw in hit_keywords:
        found = re.search(re.escape(kw), text, re.IGNORECASE)
        if found:
            idx = found.start()
            ranges.append((max(0, idx - window), min(len(text), idx + len(kw) + window)))
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    windows = [_window(text, start, end) for start, end in merged]
    joined = " ... ".join(windows) if windows else _window(text, 0, min(len(text), limit))
    return _scrub(luna.mask_pii(joined))[:limit]


LIGHT_QUESTION = "Which one of these warning lights does the caller describe?"
FLAGS_QUESTION = "Which of these facts about the car does the caller's description state?"
OPTION_TEXT_MAX = 200
INFERABLE_FLAGS = ("repeat_problem", "recent_long_service", "near_service_interval", "fresh_handover",
                   "no_pre_delivery_test")
FLAG_TEXTS = {
    "repeat_problem": "The same problem or warning light has happened on this car before.",
    "recent_long_service": "The car recently came back from a long stay at a garage.",
    "near_service_interval": "The car is due, or nearly due, for its scheduled service.",
    "fresh_handover": "The customer received the car only recently.",
    "no_pre_delivery_test": "The car was handed over without a pre-delivery test.",
}
# History cues ("again", "third time", "garage") often lie outside 40 characters of the light words.
FLAGS_WINDOW, FLAGS_LIMIT = 120, 400


def _choice_names(recs: list[dict], language: str) -> list[str]:
    """light_name per record; names that collide in this language get the English record name added."""
    names = [light_name(r, language) for r in recs]
    return [f"{n} ({r.get('name_en') or r['id'].split('.')[-1]})" if names.count(n) > 1 else n
            for n, r in zip(names, recs)]


def _light_options(text: str, recs: list[dict]) -> tuple[str, list[luna.Option]]:
    ids = [r["id"].split(".")[-1] for r in recs]
    keywords = [kw for bare in ids for row in _TABLE if row[0] == bare for kw in (*row[1], *row[2])]
    options = []
    for rec, bare in zip(recs, ids):
        name = rec.get("name_en") or bare
        options.append(luna.Option(id=bare, name=name, text=f"{name}: {rec.get('meaning_en', '')}"[:OPTION_TEXT_MAX],
                                   words=tuple(keywords_in(bare, text))))
    return _build_snippet(text, keywords), options


def _resolved_ids(result: dict) -> list[str]:
    if result["kind"] == "single" and result["assessment"]["status"] != "not_drivable":
        return [result["rec"]["id"].split(".")[-1]]
    if result["kind"] == "ambiguous" and result["luna_ids"]:
        return [r["id"].split(".")[-1] for r in result["recs"]]
    return []


def flags_needed(text: str, cell: str, request_flags) -> list[str]:
    """The resolved light ids when a model may infer context flags, else []."""
    have = set(request_flags)
    if have - {"after_service_hours"}:
        return []
    ids = _resolved_ids(classify(text, cell, request_flags))
    if not ids or any(fired_context_rules(i, request_flags) for i in ids):
        return []
    for rule in kb.context_rules():
        missing = set(rule["when_flags"]) - have
        if (rule["when_flags"] and missing and missing <= set(INFERABLE_FLAGS)
                and rule.get("when_symptom") in (None, "", *ids)):
            return ids
    return []


def flags_for(text: str, cell: str, request_flags) -> tuple[str, list] | None:
    ids = flags_needed(text, cell, request_flags)
    if not ids:
        return None
    keywords = [kw for bare in ids for row in _TABLE if row[0] == bare for kw in (*row[1], *row[2])]
    options = [luna.Option(id=f, name=f, text=FLAG_TEXTS[f]) for f in INFERABLE_FLAGS]
    return _build_snippet(text, keywords, FLAGS_WINDOW, FLAGS_LIMIT), options


def with_inferred(request_flags, outcome) -> list[str]:
    """The request flags plus the flags a model inferred; inferred flags only ever add."""
    if isinstance(outcome, luna.ManyOutcome) and outcome.status == "chosen":
        return sorted(set(request_flags) | set(outcome.choices))
    return list(request_flags)


def choice_for(text: str, cell: str, flags) -> tuple[str, list] | None:
    result = classify(text, cell, flags)
    if result["kind"] != "ambiguous" or not result["luna_ids"]:
        return None
    return _light_options(text, result["recs"])


def _any_not_drivable(recs: list[dict]) -> bool:
    """True if any candidate is not drivable on its own record: stop_now, hard_stop, or a missing or unknown severity."""
    return any(drivability_assessment(r)["status"] == "not_drivable" for r in recs)


def _single(rec: dict, flags) -> dict:
    bare = rec["id"].split(".")[-1]
    return {"kind": "single", "rec": rec, "assessment": apply_context(drivability_assessment(rec), bare, flags)}


def classify(text: str, cell: str, flags, chosen=None) -> dict:
    """Classify a description for a resolved cell, deterministically (no LLM).

    kind "escalate" {reason}, "single" {rec, assessment}, or
    "ambiguous" {recs, precaution, luna_ids}; luna_ids is set only when the LLM may pick.
    It reads only the indicator records and the context rules.
    """
    bare_ids = match(text)
    excluded_ids = excluded(text)

    # A light described in a state its record does not cover gets no verdict, and neither
    # does any light named with it: list them all with the do-not-drive precaution, or
    # escalate when it is the only light named.
    if excluded_ids:
        named_recs = [
            rec for rec in (lookup_indicator(i, cell) for i in sorted(set(bare_ids) | set(excluded_ids)))
            if rec is not None
        ]
        if len(named_recs) >= 2:
            return {"kind": "ambiguous", "recs": named_recs, "precaution": True, "luna_ids": None}
        return {"kind": "escalate", "reason": "no_match"}

    if len(bare_ids) == 0:
        return {"kind": "escalate", "reason": "no_match"}

    if len(bare_ids) == 1:
        rec = lookup_indicator(bare_ids[0], cell)
        if rec is None:
            return {"kind": "escalate", "reason": "no_match"}
        return _single(rec, flags)

    candidate_recs = []
    for bare_id in bare_ids:
        rec = lookup_indicator(bare_id, cell)
        if rec is not None:
            candidate_recs.append(rec)

    if not candidate_recs:
        return {"kind": "escalate", "reason": "no_match"}

    if len(candidate_recs) == 1:
        return _single(candidate_recs[0], flags)

    # A candidate that is not drivable on its own record is never handed to the LLM, and
    # neither is a query on which a context rule fires.
    context_fired = any(
        fired_context_rules(rec["id"].split(".")[-1], flags) for rec in candidate_recs
    )
    not_drivable = _any_not_drivable(candidate_recs)
    if not_drivable or context_fired:
        return {"kind": "ambiguous", "recs": candidate_recs, "precaution": True, "luna_ids": None}

    if isinstance(chosen, str):
        rec = next((r for r in candidate_recs if r["id"].split(".")[-1] == chosen), None)
        if rec is not None:
            return {**_single(rec, flags), "chosen_from": [r["id"].split(".")[-1] for r in candidate_recs]}

    return {"kind": "ambiguous", "recs": candidate_recs, "precaution": False, "luna_ids": bare_ids}


def assess(text: str, cell: str, flags, chosen=None) -> dict:
    """classify, with the fleet procedure added to a single result."""
    result = classify(text, cell, flags, chosen=chosen)
    if result["kind"] == "single":
        proc_id = INDICATOR_TO_PROCEDURE.get(result["rec"]["id"].split(".")[-1])
        result["fleet_proc"] = get_fleet_procedure(proc_id) if proc_id else None
    return result


def says_do_not_drive(result: dict) -> bool:
    """True when the reply for an assess() result says do not drive (stop_now or tow verdict, or the ambiguous precaution)."""
    if result["kind"] == "single":
        return result["assessment"].get("verdict") in ("stop_now", "tow")
    if result["kind"] == "ambiguous":
        return result["precaution"] or any(
            rec.get("severity") == "stop_now" or rec.get("hard_stop") is True for rec in result["recs"])
    return False


def stop_warning(text: str, flags) -> bool:
    """True when, for any vehicle carmind covers, the description gets a reply that says do not drive."""
    text = (text or "").strip()
    return bool(text) and any(says_do_not_drive(classify(text, cell, flags)) for cell in sheet_cells())


def _names_a_stop_light(text: str, flags) -> bool:
    """True when the text names a light that is not drivable on its own record, a light on which a context rule fires, or a light in a state its record does not cover together with another light.

    When the records cannot be read, any named light counts.
    """
    named = set(match(text)) | set(excluded(text))
    if not named:
        return False
    # A light described in a state its record does not cover, named with any other light,
    # gets the do-not-drive precaution (as classify does).
    if excluded(text) and len(named) >= 2:
        return True
    try:
        for rec in kb.indicators().values():
            bare = rec["id"].split(".")[-1]
            if bare in named and (drivability_assessment(rec)["status"] == "not_drivable"
                                  or fired_context_rules(bare, flags)):
                return True
        return False
    except Exception:
        return True


def stop_warning_or_fallback(text, flags) -> bool:
    """stop_warning for the error replies; it never raises.

    When stop_warning fails, empty text gets no precaution, text that names a stop light gets it, and text
    that cannot be read for lights gets it too.
    """
    if not isinstance(text, str):
        text = ""
    try:
        return stop_warning(text, flags)
    except Exception as exc:
        logger.error("stop warning failed: %s", type(exc).__name__)
        if not text.strip():
            return False
        try:
            return _names_a_stop_light(text, flags)
        except Exception:
            return True


def answer(req: ChatRequest) -> str:
    """
    Answer the latest user message.

    The vehicle comes from the caller number and the plate number field (tools.vehicle_for_request),
    or from a Skoda make, model and powertrain named in the text; the text is never searched for a
    plate. With no vehicle there is no assessment, and a light that means stop for any covered
    vehicle still gets the do-not-drive line.
    An unsupported vehicle or no match escalates. One match gets a full reply. Several
    matches get a clarifying question. With LUNA_MODE=on luna may first add context flags it infers,
    which only tighten, and may pick one of several matched lights that are all drivable on their own
    record; the reply then ends with code-written paragraphs naming the inferred flags and the matched lights.
    Context flags from the request can only tighten the result (see tools.apply_context).
    A light described in a state its record does not cover gets no verdict for any light.
    """
    language = req.language or "he-IL"
    flags = req.context_flags

    user_msgs = [m for m in req.messages if m.role == "user"]
    text = user_msgs[-1].content.strip() if user_msgs else ""

    if not text:
        return compose_escalate(language, "no_match")

    v = vehicle_for_request(text, req.caller_number, req.plate)
    if v["platform_cell"] is None:
        reply = compose_escalate(language, v["reason"], v["vehicles"])
        if stop_warning(text, flags):
            reply += "\n\n" + stop_precaution(language)
        return reply
    cell = v["platform_cell"]

    deadline = luna.request_deadline()
    inferred = None
    if luna.enabled():
        found = flags_for(text, cell, flags)
        if found is not None:
            inferred = luna.select_many(FLAGS_QUESTION, found[0], found[1], deadline=deadline)
    flags = with_inferred(flags, inferred)
    inferred_any = isinstance(inferred, luna.ManyOutcome) and inferred.status == "chosen"
    try:
        tail = "\n\n" + contract_flags_line(inferred.choices, language) if inferred_any else ""

        result = assess(text, cell, flags)
        if result["kind"] == "escalate":
            return compose_escalate(language, result["reason"])
        if result["kind"] == "single":
            return compose_reply(result["rec"], result["assessment"], result["fleet_proc"], language) + tail

        ambiguous = compose_ambiguous(result["recs"], language, force_precaution=result["precaution"])
        if result["luna_ids"]:
            snippet, options = _light_options(text, result["recs"])
            outcome = luna.select(LIGHT_QUESTION, snippet, options, deadline=deadline)
            names = _choice_names(result["recs"], language)
            ids = [o.id for o in options]
            if isinstance(outcome, luna.Outcome) and outcome.status == "picked" and outcome.choice in ids:
                i = ids.index(outcome.choice)
                picked = assess(text, cell, flags, chosen=outcome.choice)
                if picked["kind"] == "single":
                    return (compose_reply(picked["rec"], picked["assessment"], picked["fleet_proc"], language) + tail
                            + "\n\n" + contract_choice_line("picked", names[i], names, options[i].words, language))
            if isinstance(outcome, luna.Outcome) and outcome.status == "asked":
                return ambiguous + tail + "\n\n" + contract_choice_line("asked", None, names, (), language)
        return ambiguous + tail
    except Exception as exc:
        if not inferred_any:
            raise  # without inference the existing main._error_reply path is unchanged
        logger.error("answer after context inference failed: %s", type(exc).__name__)
        reply = compose_escalate(language, "error")
        if stop_warning_or_fallback(text, flags):
            reply += "\n\n" + stop_precaution(language)
        return reply
