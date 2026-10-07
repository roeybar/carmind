"""Call-desk tools served over MCP: customer, warning-light assessment, company procedure, dispatch. Deterministic and read-only."""
import copy
from typing import get_args

from app import kb
from app.compose import (
    compose_ambiguous,
    compose_escalate,
    stop_precaution,
    verdict_text,
)
from app.models import ContextFlag
from app.orchestrator import choice_for, classify, flags_for, stop_warning
from app.tools import field_given, vehicle_for_request

PROCEDURE_FOR_STATUS = {
    "not_drivable": "not_drivable",
    "drivable_caution": "drivable_warning",
    "drivable": "drivable_warning",
    "unclear": "unclear_description",
    "unsupported": "unsupported_car",
}


def car_label(car: dict) -> str:
    return f"{car['year']} {car['make']} {car['model']} ({car['powertrain']})"


# Reasons without an assessment that name a car or a customer carmind cannot assess; the others are unclear.
UNSUPPORTED_REASONS = ("plate_not_customers", "customer_mismatch", "vehicle_mismatch", "non_supported_vehicle")

_ACTION_RANK = {"none": 0, "dealer_appointment": 1, "roadside_rescue": 2, "tow_to_dealer": 3}


def request_flags(after_hours, context_flags=None) -> list[str]:
    """after_service_hours from after_hours, plus the known flags among context_flags; unknown values are ignored."""
    known = set(get_args(ContextFlag))
    extra = ([f for f in context_flags if isinstance(f, str) and f in known]
             if isinstance(context_flags, (list, tuple)) else [])
    return sorted(set((["after_service_hours"] if after_hours else []) + extra))


def _unknown_customer(customer_id: str) -> dict:
    return {"error": "unknown_customer", "customer_id": customer_id}


def _lookup(customer_id, description, caller_number, plate) -> dict:
    return vehicle_for_request(description, caller_number, plate, customer_id, text_route=False)


def get_customer(customer_id: str, caller_number="", plate="") -> dict:
    """Return the rental customer's record: name, phone number, rented cars with their plates, coverage level and current location in Fernhollow. Without a customer id, the caller number or the plate number selects the customer."""
    v = _lookup(customer_id, "", caller_number, plate)
    if v["customer"] is None:
        return _unknown_customer(customer_id)
    return copy.deepcopy(v["customer"])


def _assess(customer_id, description, after_hours, caller_number, plate, light, context_flags) -> dict:
    v = _lookup(customer_id, description, caller_number, plate)
    if field_given(customer_id) and v["reason"] == "unknown_customer":
        return _unknown_customer(customer_id)
    flags = request_flags(after_hours, context_flags)
    warn = stop_warning(description, flags)
    customer, car, cell = v["customer"], v["vehicle"], v["platform_cell"]

    out = {"customer_id": customer["id"] if customer else None, "vehicle": car_label(car) if car else None,
           "status": "unclear", "indicator": None, "severity": None, "verdict_text": "", "source": None}
    if cell is None:
        out["status"] = "unsupported" if v["reason"] in UNSUPPORTED_REASONS else "unclear"
        out["verdict_text"] = compose_escalate("en-US", v["reason"], v["vehicles"], text_route=False)
        if warn:
            out["status"] = "not_drivable"
            out["verdict_text"] += "\n\n" + stop_precaution("en-US")
        return out
    if not description.strip():
        out["verdict_text"] = compose_escalate("en-US", "no_match")
        return out

    result = classify(description.strip(), cell, flags, chosen=light or None)
    if result["kind"] == "escalate":
        out["verdict_text"] = compose_escalate("en-US", result["reason"])
    elif result["kind"] == "ambiguous":
        out["status"] = "not_drivable" if result["precaution"] else "unclear"
        out["verdict_text"] = compose_ambiguous(result["recs"], "en-US", force_precaution=result["precaution"])
    else:
        rec, assessment = result["rec"], result["assessment"]
        prov = rec.get("provenance", {})
        out["status"] = assessment["status"]
        out["indicator"] = rec["id"].split(".")[-1]
        out["severity"] = assessment["severity"]
        out["verdict_text"] = verdict_text(rec, assessment)
        out["source"] = f"{prov.get('manual_id', '')} p.{prov.get('source_page', '')}"
    return out


def assess_warning_light(customer_id: str, description: str, after_hours: bool = False, caller_number="",
                         plate="", light: str = "") -> dict:
    """Assess a dashboard warning light for the caller's rented car with carmind's deterministic rules. Returns the drivability status, severity and the verdict text the operator reads out. The car comes from the customer id, the caller number or the plate number, never from the description. The verdict is final. light is optional. When the description matches several lights that are all drivable on their own record, none is described in a state its record does not cover and no context rule fires, a light named here that is one of them is the one assessed; any other value is ignored. carmind's agent fills it in from its constrained choice."""
    return _assess(customer_id, description, after_hours, caller_number, plate, light, None)


def get_company_procedure(situation: str) -> dict:
    """Return the company's steps for a situation: not_drivable, drivable_warning, unclear_description, unsupported_car or after_hours."""
    procedures = kb.company_policy()["procedures"]
    if situation not in procedures:
        return {"error": "unknown_situation", "known": sorted(procedures)}
    return {"situation": situation, "steps": list(procedures[situation])}


def _plan(customer_id, description, after_hours, caller_number, plate, light, context_flags) -> dict:
    v = _lookup(customer_id, description, caller_number, plate)
    customer = v["customer"]
    if customer is None:
        return _unknown_customer(customer_id)
    assessed = _assess(customer_id, description, after_hours, caller_number, plate, light, context_flags)
    if "error" in assessed:
        return assessed
    status = assessed["status"]
    zone = customer["location"]["zone"]
    model = kb.dispatch_model()
    rule = next((r for r in model["rules"] if r["status"] == status and r["zone"] in (None, zone)), None)
    if rule is None:
        raise ValueError(f"no dispatch rule for status {status!r} in zone {zone!r}")
    action = rule["action"]
    level = next(lv for lv in kb.company_policy()["coverage_levels"] if lv["id"] == customer["coverage"])
    procedures = [PROCEDURE_FOR_STATUS[status]] + (["after_hours"] if after_hours else [])
    policy_procedures = kb.company_policy()["procedures"]
    return {
        "customer_id": customer_id,
        "status": status,
        "action": action,
        "provider": model["providers"].get(action),
        "destination": model["dealer"] if action == "tow_to_dealer" else None,
        "coverage": customer["coverage"],
        "covered": action == "none" or action in level["pays_for"],
        "procedures": procedures,
        "steps": [step for name in procedures for step in policy_procedures[name]],
    }


def plan_dispatch(customer_id: str, description: str, after_hours: bool = False, caller_number="", plate="") -> dict:
    """Choose the dispatch (none, roadside_rescue, tow_to_dealer or dealer_appointment) from the verdict, the coverage and the location, with the provider and the company steps to follow."""
    return _plan(customer_id, description, after_hours, caller_number, plate, "", None)


def more_conservative(unpicked: dict, picked: dict) -> dict:
    """The higher action (_ACTION_RANK), the covered plan on a tie, and never less coverage than unpicked."""
    if "error" in unpicked:
        return unpicked
    if "error" in picked:
        return picked
    ru, rp = _ACTION_RANK.get(unpicked["action"]), _ACTION_RANK.get(picked["action"])
    if ru is None or rp is None:
        raise ValueError("unknown dispatch action")
    chosen = picked if rp > ru or (rp == ru and picked["covered"] and not unpicked["covered"]) else unpicked
    if unpicked["covered"] and not chosen["covered"]:
        raise ValueError("a more conservative dispatch is not covered")
    return chosen


def assess_for_agent(customer_id, description, after_hours, caller_number, plate, light, context_flags) -> dict:
    """The assessment the agent's reply shows, with luna's light and inferred context flags. Not an MCP tool."""
    return _assess(customer_id, description, after_hours, caller_number, plate, light, context_flags)


def dispatch_for_agent(customer_id, description, after_hours, caller_number, plate, light, context_flags) -> dict:
    """The dispatch the agent's reply shows: with the inferred flags, and with a light the more conservative of
    the plans with and without it. Not an MCP tool."""
    unpicked = _plan(customer_id, description, after_hours, caller_number, plate, "", context_flags)
    if not light:
        return unpicked
    return more_conservative(unpicked, _plan(customer_id, description, after_hours, caller_number, plate, light,
                                             context_flags))


def _cell_for(customer_id, description, caller_number, plate):
    v = _lookup(customer_id, description, caller_number, plate)
    if field_given(customer_id) and v["reason"] == "unknown_customer":
        return None
    return v["platform_cell"] if description.strip() else None


def light_choice(customer_id, description, after_hours=False, caller_number="", plate="", context_flags=None):
    """(snippet, options) for luna's light question on the caller's car, or None. Not an MCP tool."""
    cell = _cell_for(customer_id, description, caller_number, plate)
    return None if cell is None else choice_for(description.strip(), cell, request_flags(after_hours, context_flags))


def flags_choice(customer_id, description, after_hours=False, caller_number="", plate=""):
    """(context, options) for luna's context question on the caller's car, or None. Not an MCP tool."""
    cell = _cell_for(customer_id, description, caller_number, plate)
    return None if cell is None else flags_for(description.strip(), cell, request_flags(after_hours))


TOOLS = {
    "get_customer": get_customer,
    "assess_warning_light": assess_warning_light,
    "get_company_procedure": get_company_procedure,
    "plan_dispatch": plan_dispatch,
}
