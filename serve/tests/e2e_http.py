"""
End-to-end check against a running service (SERVE_URL).

Sends the he-IL brake-fluid query and checks for the layered reply with provenance
and a do-not-drive line, plus the en-US /assistant route, non-petrol Skoda rejection (including
"\u0160koda"), a light in a state its record excludes (alone: escalate; with another light: clarifying
question with the do-not-drive precaution), BMW and RAV4 rejection, the vehicle checks (no caller number
or plate, in English and Hebrew, with the do-not-drive line for a stop light; a plate typed with spaces; an unknown
plate; a plate and a named car that disagree; a plate in the text that is not read; a caller on record with one car,
with two cars, with two cars and one of their plates, with another customer's plate; an unknown caller; an info light
without the do-not-drive line; a phone number in capitals; MG4 as a make), then the demo: the page, the four
customers, the agent route (a plate in the description is not read, the caller number and the plate number field,
no customer, a caller number that does not belong to the customer, an unknown customer with a stop light) and the
MCP service. With luna on (the compose default) it first checks the code-written light and context paragraphs on
POST /, and the light choice, the inferred context and the dispatch on the agent route; a second preflight request
aborts the run when the app answers with luna off. Exits 0 on pass, 1 on fail.
"""
import asyncio
import os
import sys
import time

import httpx

from app.compose import compose_escalate, stop_precaution

SERVE_URL = os.environ.get("SERVE_URL", "http://localhost:7600")
FORBIDDEN = ["safe to drive", "בטוח לנסיעה"]


def wait_for_service(max_wait: int = 30) -> None:
    start = time.time()
    last_exc = None
    while time.time() - start < max_wait:
        try:
            r = httpx.get(f"{SERVE_URL}/health", timeout=2)
            if r.status_code == 200:
                print(f"[e2e] Service ready at {SERVE_URL}")
                return
        except Exception as exc:
            last_exc = exc
        time.sleep(1)
    raise RuntimeError(
        f"Service not ready at {SERVE_URL} after {max_wait}s. Last error: {last_exc}"
    )


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f": {detail}" if detail else ""))
    return condition


def run_e2e() -> bool:
    wait_for_service()
    # Two agent requests; the rig never runs against a paid provider.
    from stress import luna_preflight, provider_preflight
    problem = provider_preflight()
    if problem:
        print(f"[e2e] ABORTED: {problem}")
        sys.exit(1)
    problem = luna_preflight()
    if problem:
        print(f"[e2e] ABORTED: {problem}")
        sys.exit(1)
    passed = run_luna_checks()

    # Golden query: he-IL red brake
    resp = httpx.post(f"{SERVE_URL}/", json={
        "language": "he-IL",
        "messages": [{"role": "user", "content": "נורת נוזל הבלמים דלוקה"}], "plate": "FH-104-27",
    }, timeout=10)

    passed &= check("he-IL POST / returns 200", resp.status_code == 200)
    reply = resp.json().get("reply", "")

    passed &= check("Section A rulebook label present", "מדריך הצי" in reply, reply[:80])
    passed &= check("Section A attribution present", "מקור: נוהל הצי" in reply, reply[:80])
    passed &= check("Section B carmind label present", "הערכת carmind" in reply, reply[:80])
    passed &= check("Rulebook before carmind", reply.find("מדריך הצי") < reply.find("הערכת carmind"), "")
    passed &= check("Provenance page 127 present", "127" in reply, reply)
    passed &= check("stop_now severity present", "stop_now" in reply, reply)
    passed &= check("Do-not-drive line present", "אין לנסוע" in reply, reply)

    for phrase in FORBIDDEN:
        passed &= check(f"No '{phrase}'", phrase not in reply.lower(), reply[:80])

    # /health
    hresp = httpx.get(f"{SERVE_URL}/health", timeout=5)
    passed &= check("GET /health 200", hresp.status_code == 200)

    # en-US route
    eresp = httpx.post(f"{SERVE_URL}/assistant", json={
        "language": "en-US",
        "messages": [{"role": "user", "content": "brake fluid warning light"}], "plate": "FH-104-27",
    }, timeout=10)
    passed &= check("en-US /assistant 200", eresp.status_code == 200)
    ereply = eresp.json().get("reply", "")
    passed &= check("en-US rulebook label", "Fleet recovery guide:" in ereply, ereply[:80])
    passed &= check("en-US Do not drive", "Do not drive." in ereply, ereply[:80])
    passed &= check("en-US no forbidden", all(p not in ereply.lower() for p in FORBIDDEN), ereply[:80])

    # Non-petrol Skoda and a state the record excludes: no Skoda verdict
    hresp2 = httpx.post(f"{SERVE_URL}/", json={
        "language": "he-IL",
        "messages": [{"role": "user", "content": "סקודה סופרב נורת מנוע"}],
    }, timeout=10)
    hreply2 = hresp2.json().get("reply", "")
    passed &= check("he-IL non-petrol Skoda rejected",
                    hreply2.startswith("לא נמצא מידע עבור כלי הרכב"), hreply2[:80])

    dresp = httpx.post(f"{SERVE_URL}/", json={
        "language": "en-US",
        "messages": [{"role": "user", "content": "Skoda Octavia diesel check engine light"}],
    }, timeout=10)
    dreply = dresp.json().get("reply", "")
    passed &= check("en-US diesel Skoda rejected",
                    dreply.startswith("No information found for the specified vehicle."), dreply[:80])

    sresp = httpx.post(f"{SERVE_URL}/", json={
        "language": "en-US",
        "messages": [{"role": "user", "content": "ESC light stays on continuously, not flashing"}], "plate": "FH-104-27",
    }, timeout=10)
    sreply = sresp.json().get("reply", "")
    passed &= check("en-US excluded ESC state gets no record verdict",
                    sreply.startswith("We could not identify the dashboard light"), sreply[:80])

    xresp = httpx.post(f"{SERVE_URL}/", json={
        "language": "en-US",
        "messages": [{"role": "user", "content": "coolant warning and engine light on, engine overheating"}], "plate": "FH-104-27",
    }, timeout=10)
    xreply = xresp.json().get("reply", "")
    passed &= check("en-US excluded state with another light is conservative",
                    xreply.startswith("Multiple possible warning lights identified:")
                    and "do not drive" in xreply.lower(), xreply[:80])

    eresp = httpx.post(f"{SERVE_URL}/", json={
        "language": "en-US",
        "messages": [{"role": "user", "content": "\u0160koda Enyaq ESC light flashing"}],
    }, timeout=10)
    ereply = eresp.json().get("reply", "")
    passed &= check("en-US \u0160koda Enyaq rejected",
                    ereply.startswith("No information found for the specified vehicle."), ereply[:80])

    for text in ("BMW check engine light", "Toyota RAV4 check engine light"):
        eresp = httpx.post(f"{SERVE_URL}/", json={
            "language": "en-US",
            "messages": [{"role": "user", "content": text}],
        }, timeout=10)
        ereply = eresp.json().get("reply", "")
        passed &= check(f"en-US {text.split()[0]} rejected",
                        ereply.startswith("No information found for the specified vehicle."), ereply[:80])

    def user(text):
        return {"role": "user", "content": text}

    en_stop = "\n\n" + stop_precaution("en-US")
    he_red = "\u05e0\u05d5\u05e8\u05ea \u05e0\u05d5\u05d6\u05dc \u05d4\u05d1\u05dc\u05de\u05d9\u05dd \u05d3\u05dc\u05d5\u05e7\u05d4"
    red = "red brake warning light"
    esc = "esc stability control light is flashing"
    vehicle_checks = [
        ("en-US no caller number or plate gets the plate question and the precaution", "en-US", red, {},
         lambda r: r == compose_escalate("en-US", "vehicle_unknown") + en_stop),
        ("he-IL no caller number or plate gets the plate question and the precaution", "he-IL", he_red, {},
         lambda r: r == compose_escalate("he-IL", "vehicle_unknown") + "\n\n" + stop_precaution("he-IL")),
        ("he-IL plate typed with spaces gets the verdict", "he-IL", he_red, {"plate": "fh 104 27"},
         lambda r: "\u05d0\u05d9\u05df \u05dc\u05e0\u05e1\u05d5\u05e2" in r and "\u05de\u05d3\u05e8\u05d9\u05da \u05d4\u05e6\u05d9" in r),
        ("en-US unknown plate gets no assessment and the precaution", "en-US", red, {"plate": "FH-999-99"},
         lambda r: r == compose_escalate("en-US", "plate_unknown") + en_stop),
        ("en-US plate and named car disagree", "en-US", "Toyota RAV4 " + red, {"plate": "FH-104-27"},
         lambda r: r == compose_escalate("en-US", "vehicle_mismatch") + en_stop),
        ("en-US a plate in the text is not read", "en-US", "FH-104-27 " + red, {},
         lambda r: r == compose_escalate("en-US", "vehicle_unknown") + en_stop),
        ("en-US caller with one car gets the verdict", "en-US", red, {"caller_number": "+1-555-0142"},
         lambda r: "Do not drive." in r),
        ("en-US caller with two cars is asked which one", "en-US", red, {"caller_number": "+1-555-0177"},
         lambda r: "FH-452-63" in r and "FH-587-14" in r and "FH-104-27" not in r
         and r.startswith("This customer has more than one car on record") and r.endswith(en_stop)),
        ("en-US caller with two cars and one of their plates gets the verdict", "en-US", red,
         {"caller_number": "+1-555-0177", "plate": "FH-452-63"}, lambda r: "Do not drive." in r),
        ("en-US caller with another customer's plate gets no assessment", "en-US", red,
         {"caller_number": "+1-555-0177", "plate": "FH-104-27"},
         lambda r: r == compose_escalate("en-US", "plate_not_customers") + en_stop),
        ("en-US unknown caller gets no assessment and the precaution", "en-US", red, {"caller_number": "+1-555-0199"},
         lambda r: r == compose_escalate("en-US", "unknown_caller") + en_stop),
        ("en-US info light for a caller on record has no do-not-drive line", "en-US", esc,
         {"caller_number": "+1-555-0142"}, lambda r: "drive" not in r),
        ("en-US info light without a vehicle gets only the plate question", "en-US", esc, {},
         lambda r: r == compose_escalate("en-US", "vehicle_unknown")),
        # Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
        ("en-US a phone number written in capitals keeps the verdict", "en-US", red + ", TEL 555-0123",
         {"plate": "FH-104-27"}, lambda r: "Do not drive." in r),
        ("en-US MG4 named for a car on record is a mismatch", "en-US", "My MG4 has the ESC light flashing",
         {"plate": "FH-104-27"}, lambda r: r == compose_escalate("en-US", "vehicle_mismatch")),
    ]
    for name, language, text, fields, ok in vehicle_checks:
        presp = httpx.post(f"{SERVE_URL}/", json={"language": language, "messages": [user(text)], **fields}, timeout=10)
        preply = presp.json().get("reply", "")
        passed &= check(name, presp.status_code == 200 and ok(preply), preply[:80])

    passed &= run_demo_checks()
    return passed


def run_luna_checks() -> bool:
    from app.providers import FAKE_FLAGS_REASON, FAKE_PICK_REASON
    from stress import (
        FLAG_FIXTURES,
        LD_EN,
        LF_EN,
        LP_EN,
        LUNA_FIXTURES,
        luna_reply_problems,
    )
    passed = True
    for fixture in (LUNA_FIXTURES[0], LUNA_FIXTURES[2], FLAG_FIXTURES[0], LUNA_FIXTURES[3]):
        resp = httpx.post(f"{SERVE_URL}/", json=fixture, timeout=30)
        reply = resp.json().get("reply", "")
        problems = luna_reply_problems(fixture, reply)
        passed &= check(f"luna POST / {fixture['messages'][0]['content'][:40]!r} {fixture.get('context_flags', [])}",
                        resp.status_code == 200 and not problems, f"{problems} {reply[:80]}")
    resp = httpx.post(f"{SERVE_URL}/", json={"language": "en-US", "plate": "FH-104-27", "messages": [
        {"role": "user", "content": "brake fluid light and coolant light both on"}]}, timeout=30)
    reply = resp.json().get("reply", "")
    passed &= check("luna POST / a stop light keeps the precaution and no light is picked",
                    "do not drive" in reply.lower() and "Light picked" not in reply, reply[:80])

    def agent(customer_id, problem):
        return httpx.post(f"{SERVE_URL}/api/agent", json={"customer_id": customer_id, "problem": problem},
                          timeout=30).json()

    data = agent("C-1002", LP_EN)
    passed &= check("luna agent C-1002: the coolant light is picked", data.get("choice") == {
        "status": "picked", "picked": "coolant_level_low",
        "options": [{"id": "coolant_level_low", "name": "Coolant level too low"},
                    {"id": "engine_control_petrol", "name": "Petrol engine control faulty"}],
        "reasoning": FAKE_PICK_REASON, "withheld": False}, str(data.get("choice"))[:120])
    passed &= check("luna agent C-1002: the verdict is the coolant record",
                    (data.get("verdict") or {}).get("indicator") == "coolant_level_low")
    passed &= check("luna agent C-1002: the dispatch is the plan for an unconfirmed light",
                    (data.get("dispatch") or {}).get("status") == "unclear", str(data.get("dispatch"))[:120])
    data = agent("C-1001", LD_EN)
    dispatch = data.get("dispatch") or {}
    passed &= check("luna agent C-1001: picked, roadside rescue, covered",
                    (data.get("choice") or {}).get("status") == "picked" and dispatch.get("action") == "roadside_rescue"
                    and dispatch.get("covered") is True, str(dispatch)[:120])
    data = agent("C-1002", LF_EN)
    passed &= check("luna agent C-1002: repeat_problem is inferred", data.get("context") == {
        "status": "chosen", "flags": ["repeat_problem"],
        "options": ["repeat_problem", "recent_long_service", "near_service_interval", "fresh_handover",
                    "no_pre_delivery_test"],
        "reasoning": FAKE_FLAGS_REASON, "withheld": False}, str(data.get("context"))[:120])
    passed &= check("luna agent C-1002: the inferred repeat is a tow",
                    (data.get("verdict") or {}).get("status") == "not_drivable"
                    and (data.get("dispatch") or {}).get("action") == "tow_to_dealer")
    data = agent("C-1001", "red brake warning light")
    passed &= check("luna agent: a stop light makes no choice and no inference",
                    data.get("context") is None and data.get("choice") is None)
    return passed


def _probe_mcp() -> dict:
    """Probe the MCP service, retrying for up to 30 s while it starts."""
    from app.mcp_probe import probe
    deadline = time.time() + 30
    while True:
        try:
            return asyncio.run(probe(os.environ["CARMIND_MCP_URL"], "C-1001", "red brake warning light"))
        except Exception:
            if time.time() >= deadline:
                raise
            time.sleep(1)


def run_demo_checks() -> bool:
    passed = True
    page = httpx.get(f"{SERVE_URL}/", timeout=10)
    passed &= check("GET / serves the page", page.status_code == 200 and 'id="ask"' in page.text)
    passed &= check("the page is ASCII", page.content.isascii())
    customers = httpx.get(f"{SERVE_URL}/api/customers", timeout=10).json().get("customers", [])
    passed &= check("/api/customers lists 4", len(customers) == 4, str(len(customers)))

    resp = httpx.post(f"{SERVE_URL}/api/agent", json={"customer_id": "C-1001", "problem": "red brake warning light"},
                      timeout=30)
    data = resp.json()
    passed &= check("agent: 200 and not_drivable", resp.status_code == 200 and data["verdict"]["status"] == "not_drivable")
    passed &= check("agent: do-not-drive verdict text", "Do not drive." in data["verdict"]["text"], data["verdict"]["text"])
    passed &= check("agent: assess_warning_light was called",
                    "assess_warning_light" in [c["name"] for c in data["tool_calls"]])
    procedure = [c for c in data["tool_calls"] if c["name"] == "get_company_procedure"]
    passed &= check("agent: get_company_procedure was called by the model",
                    len(procedure) == 1 and procedure[0]["by"] == "model"
                    and procedure[0]["summary"].startswith("not_drivable: "), str(procedure))
    passed &= check("agent: tow to dealer", (data["dispatch"] or {}).get("action") == "tow_to_dealer")
    passed &= check("agent: provider is fake", data["provider"] == "fake", str(data["provider"]))

    resp = httpx.post(f"{SERVE_URL}/api/agent", json={"customer_id": "C-1003", "problem": "red brake warning light"},
                      timeout=30)
    passed &= check("agent: C-1003 stop light is not drivable", resp.json()["verdict"]["status"] == "not_drivable")
    resp = httpx.post(f"{SERVE_URL}/api/agent", json={"customer_id": "C-1003", "problem": "check engine light"},
                      timeout=30)
    passed &= check("agent: C-1003 unsupported", resp.json()["verdict"]["status"] == "unsupported")

    resp = httpx.post(f"{SERVE_URL}/api/agent", json={"customer_id": "C-1001", "problem": "BMW check engine light"},
                      timeout=30)
    passed &= check("agent: described BMW unsupported", resp.json()["verdict"]["status"] == "unsupported")

    resp = httpx.post(f"{SERVE_URL}/api/agent",
                      json={"customer_id": "C-1001", "problem": "red brake warning light at 60000 km"}, timeout=30)
    passed &= check("agent: mileage is not a plate", resp.json()["verdict"]["status"] == "not_drivable")

    resp = httpx.post(f"{SERVE_URL}/api/agent",
                      json={"customer_id": "C-1001", "problem": "red brake warning light, a jeep hit me"}, timeout=30)
    passed &= check("agent: a lowercase word that is also a make does not void the car",
                    resp.json()["verdict"]["status"] == "not_drivable")
    resp = httpx.post(f"{SERVE_URL}/api/agent",
                      json={"customer_id": "C-1001", "problem": "red brake warning light, the view is superb"}, timeout=30)
    passed &= check("agent: a model name used as an ordinary word does not void the car",
                    resp.json()["verdict"]["status"] == "not_drivable")
    resp = httpx.post(f"{SERVE_URL}/api/agent",
                      json={"customer_id": "C-1001", "problem": "FH-318-05-8 red brake warning light"}, timeout=30)
    passed &= check("agent: a plate in the description is not read",
                    resp.json()["verdict"]["status"] == "not_drivable")
    # Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
    resp = httpx.post(f"{SERVE_URL}/api/agent",
                      json={"customer_id": "C-1001", "problem": "RED BRAKE WARNING LIGHT, CALL ME 555 0123"}, timeout=30)
    passed &= check("agent: a phone number written in capitals does not void the car",
                    resp.json()["verdict"]["status"] == "not_drivable")
    resp = httpx.post(f"{SERVE_URL}/api/agent",
                      json={"customer_id": "C-1001", "problem": "red brake warning light, my other car plate:AB-123-45"}, timeout=30)
    passed &= check("agent: a plate in the description is not read (joined to a word)",
                    resp.json()["verdict"]["status"] == "not_drivable")
    for name, body, status, needle in (
        ("agent: a caller with two cars and a stop light is not drivable",
         {"caller_number": "+1-555-0177", "problem": "red brake warning light"}, "not_drivable", "Do not drive"),
        ("agent: the caller number and the plate pick the car",
         {"caller_number": "+1-555-0177", "plate": "FH-452-63", "problem": "red brake warning light"},
         "not_drivable", "Do not drive"),
        ("agent: another customer's plate is unsupported",
         {"caller_number": "+1-555-0177", "plate": "FH-104-27", "problem": "check engine light"}, "unsupported", ""),
        ("agent: no caller number or plate is unclear", {"problem": "check engine light"}, "unclear", ""),
        ("agent: a caller number that does not belong to the customer",
         {"customer_id": "C-1001", "caller_number": "+1-555-0118", "problem": "red brake warning light"},
         "not_drivable", "does not belong"),
    ):
        resp = httpx.post(f"{SERVE_URL}/api/agent", json=body, timeout=30)
        verdict = resp.json().get("verdict") or {}
        passed &= check(name, resp.status_code == 200 and verdict.get("status") == status
                        and needle in verdict.get("text", ""), str(verdict)[:120])
    resp = httpx.post(f"{SERVE_URL}/api/agent", json={"customer_id": "C-9999", "problem": "red brake warning light"},
                      timeout=30)
    passed &= check("agent: an unknown customer with a stop light gets 400 and the precaution",
                    resp.status_code == 400 and resp.json() == {"error": "unknown customer",
                                                                "precaution": stop_precaution("en-US")}, resp.text[:120])

    try:
        found = _probe_mcp()
        passed &= check("mcp: the four tools", set(found["tools"]) == {
            "get_customer", "assess_warning_light", "get_company_procedure", "plan_dispatch"}, str(found["tools"]))
        passed &= check("mcp: not_drivable at stop_now",
                        found["result"]["status"] == "not_drivable" and found["result"]["severity"] == "stop_now")
    except Exception as exc:
        passed &= check("mcp: probe", False, type(exc).__name__)
    return passed


if __name__ == "__main__":
    try:
        ok = run_e2e()
    except Exception as exc:
        print(f"[e2e] FATAL: {exc}")
        sys.exit(1)

    if ok:
        print("\n[e2e] ALL PASS")
        sys.exit(0)
    else:
        print("\n[e2e] FAILURES, see above")
        sys.exit(1)
