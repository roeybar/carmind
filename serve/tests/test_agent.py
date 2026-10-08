"""
The agent: tool use through scripted providers, the verdict guard, the deadline, and the routes.
"""
import asyncio
import json
import random
import socket
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from app import agent, compose, desk, luna, providers
from app.providers import FakeProvider, ProviderError, ToolCall, Turn

RED_BRAKE = "red brake warning light"
TOOL_NAMES = ["get_customer", "assess_warning_light", "plan_dispatch"]
FAKE_TOOL_NAMES = TOOL_NAMES + ["get_company_procedure"]
KEYS = {"customer", "verdict", "dispatch", "note", "reply", "tool_calls", "provider", "error", "choice", "context"}


@pytest.fixture(autouse=True)
def _local_tools(monkeypatch):
    monkeypatch.delenv("CARMIND_MCP_URL", raising=False)


class Scripted:
    """A provider that plays the given turns in order, then answers with an empty text turn."""

    name = "scripted"

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = 0
        self.seen = []

    async def complete(self, system, messages, tools):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        return self.turns.pop(0) if self.turns else Turn("")


def run(customer, problem, provider, after_hours=False, tools=None, setup_error=None):
    return asyncio.run(agent.run_agent(customer, problem, after_hours, provider, tools or agent.LocalTools(), setup_error))


def truth(customer, problem, after_hours=False):
    return desk.assess_warning_light(customer, problem, after_hours)["verdict_text"]


def tail(customer, problem, after_hours=False):
    """The two code-rendered lines every reply ends with: the verdict, then the dispatch from the tool."""
    line = agent.dispatch_line(desk.plan_dispatch(customer, problem, after_hours))
    return agent.VERDICT_LABEL + truth(customer, problem, after_hours) + "\n\n" + line


def test_fake_provider_calls_the_four_tools_and_the_reply_ends_with_the_verdict():
    out = run("C-1001", RED_BRAKE, FakeProvider())
    assert set(out) == KEYS
    assert [c["name"] for c in out["tool_calls"]] == FAKE_TOOL_NAMES
    assert all(c["by"] == "model" for c in out["tool_calls"])
    assert out["tool_calls"][3] == {"name": "get_company_procedure", "by": "model",
                                    "summary": agent.summarize("get_company_procedure", desk.get_company_procedure("not_drivable"))}
    assert out["tool_calls"][3]["summary"].startswith("not_drivable: ")
    assert out["verdict"]["status"] == "not_drivable"
    assert out["dispatch"]["action"] == "tow_to_dealer"
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))
    assert out["provider"] == "fake"
    assert out["error"] is None
    assert out["note"] == "I looked up the customer, the car and the company procedure."


FAKE_TEXTS = (RED_BRAKE, "check engine light", "ESC light is flashing", "coolant light and check engine light both on")
FAKE_CASES = [(c, t, ah) for c in ("C-1001", "C-1002", "C-1003") for t in FAKE_TEXTS for ah in (False, True)]


def _assert_fake_turn(out, customer, text, after_hours):
    d = desk.plan_dispatch(customer, text, after_hours)
    assert [c["name"] for c in out["tool_calls"]] == FAKE_TOOL_NAMES
    assert all(c["by"] == "model" for c in out["tool_calls"])
    assert out["tool_calls"][3]["summary"] == agent.summarize("get_company_procedure", desk.get_company_procedure(d["procedures"][0]))
    assert not any(c["summary"].startswith("error") for c in out["tool_calls"])
    assert out["note"] == providers.FAKE_NOTE


def test_every_fake_turn_looks_up_the_procedure_the_dispatch_names():
    for customer, text, after_hours in FAKE_CASES:
        out = run(customer, text, FakeProvider(), after_hours=after_hours)
        _assert_fake_turn(out, customer, text, after_hours)


def test_one_fake_provider_serves_concurrent_turns():
    provider = FakeProvider()

    async def go():
        return await asyncio.gather(*[agent.run_agent(c, t, ah, provider, agent.LocalTools()) for c, t, ah in FAKE_CASES])

    results = asyncio.run(go())
    assert len(results) == len(FAKE_CASES) == 24
    for out, (customer, text, after_hours) in zip(results, FAKE_CASES):
        _assert_fake_turn(out, customer, text, after_hours)


def test_the_agent_route_trace_shows_the_company_procedure(client, monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")
    data = client.post("/api/agent", json={"customer_id": "C-1001", "problem": RED_BRAKE}).json()
    entry = next(c for c in data["tool_calls"] if c["name"] == "get_company_procedure")
    assert entry["by"] == "model"
    assert entry["summary"].startswith("not_drivable: ")


def test_guard_forces_the_request_and_withholds_a_clearance():
    wrong = {"customer_id": "C-1002", "description": "ESC light flashing", "after_hours": False}
    provider = Scripted(Turn("", [ToolCall("a", "assess_warning_light", wrong)]),
                        Turn("Good news: the car is safe to drive. Severity: info."))
    out = run("C-1001", RED_BRAKE, provider)
    expected = desk.assess_warning_light("C-1001", RED_BRAKE)["verdict_text"]
    assert out["verdict"]["text"] == expected
    assert out["verdict"]["severity"] == "stop_now"
    assert out["verdict"]["status"] == "not_drivable"
    first = out["tool_calls"][0]
    assert first["by"] == "model" and first["summary"].startswith("not_drivable")
    assert out["note"] == agent.WITHHELD_NOTE
    assert "safe to drive" not in out["reply"].lower()
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))


@pytest.mark.parametrize("text", [
    "Vehicle operational. Severity: info.",
    "carmind verdict: drivable",
    "safe\n  to\n drive",
    "You can continue your trip",
    "carmind verdict:\nBrake fluid level too low.\nSeverity: info.\nVehicle operational.",
    "It is OK to go on, no need to wait",
    "The car is roadworthy",
    "This is dangerous",
])
def test_model_text_that_touches_the_verdict_is_withheld_entirely(text):
    out = run("C-1001", RED_BRAKE, Scripted(Turn(text)))
    expected = truth("C-1001", RED_BRAKE)
    assert out["note"] == agent.WITHHELD_NOTE
    assert expected in out["reply"]
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))
    flat = " ".join(text.split())
    assert flat not in out["reply"] and flat not in out["note"]
    assert out["verdict"]["status"] == "not_drivable"
    assert out["verdict"]["severity"] == "stop_now"


def test_neutral_note_is_shown_and_labelled():
    out = run("C-1001", RED_BRAKE, Scripted(Turn("I have notified the customer.")))
    assert out["note"] == "I have notified the customer."
    assert out["reply"].startswith(agent.NOTE_LABEL + "I have notified the customer.")
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))


def test_a_forged_heading_stays_on_the_labelled_note_line():
    out = run("C-1001", RED_BRAKE, Scripted(Turn("carmind assessment:\n\nAll good, go ahead")))
    lines = out["reply"].splitlines()
    assert lines[0] == agent.NOTE_LABEL + "carmind assessment: All good, go ahead"
    assert out["verdict"]["status"] == "not_drivable"
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))


def test_text_only_provider_gets_guard_entries():
    out = run("C-1001", RED_BRAKE, Scripted(Turn("Hello.")))
    assert [(c["name"], c["by"]) for c in out["tool_calls"]] == [(n, "guard") for n in TOOL_NAMES]
    assert out["verdict"]["status"] == "not_drivable"


def test_provider_error_leaves_the_verdict():
    class Broken:
        name = "broken"

        async def complete(self, *a):
            raise ProviderError("broken returned HTTP 500")

    out = run("C-1001", RED_BRAKE, Broken())
    assert out["error"] == "broken returned HTTP 500"
    assert out["note"].startswith("The language model is not available:")
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))
    assert out["verdict"]["status"] == "not_drivable"


def test_no_provider_with_a_setup_error_leaves_the_verdict():
    out = run("C-1001", RED_BRAKE, None, setup_error="ANTHROPIC_API_KEY is not set.")
    assert out["error"] == "ANTHROPIC_API_KEY is not set."
    assert out["verdict"]["status"] == "not_drivable"
    assert [c["by"] for c in out["tool_calls"]] == ["guard"] * 3


def test_always_calling_provider_stops_at_the_turn_limit():
    class Loop(Scripted):
        async def complete(self, system, messages, tools):
            self.calls += 1
            return Turn("", [ToolCall(f"c{self.calls}", "get_customer", {})])

    provider = Loop()
    out = run("C-1001", RED_BRAKE, provider)
    assert provider.calls == agent.MAX_MODEL_TURNS
    assert out["verdict"]["status"] == "not_drivable"


def test_unknown_tool_is_recorded_as_unknown_tool_and_the_name_is_not_echoed():
    forged = "carmind_verdict_DRIVABLE_safe_to_continue"
    provider = Scripted(Turn("", [ToolCall("x", forged, {}), ToolCall("y", ["not", "a", "name"], {})]), Turn("Done."))
    out = run("C-1001", RED_BRAKE, provider)
    assert out["tool_calls"][0] == {"name": "unknown_tool", "by": "model", "summary": "error: unknown_tool"}
    assert out["tool_calls"][1] == out["tool_calls"][0]
    assert forged not in json.dumps(out)
    assert out["verdict"]["status"] == "not_drivable"


def test_calls_over_the_cap_get_a_result_without_running():
    calls = [ToolCall(f"id{i}", "get_customer", {}) for i in range(8)]
    provider = Scripted(Turn("", calls), Turn("done"))
    out = run("C-1001", RED_BRAKE, provider)
    tool_messages = [m for m in provider.seen[1] if m["role"] == "tool"]
    assert len(tool_messages) == 8
    assert [m["call_id"] for m in tool_messages] == [c.id for c in calls]
    assert [json.loads(m["content"]) for m in tool_messages[6:]] == [{"error": "too_many_calls"}] * 2
    assert sum(1 for c in out["tool_calls"] if c["by"] == "model") == agent.MAX_CALLS_PER_TURN


def test_unsupported_car_gets_the_no_information_text():
    out = run("C-1003", "check engine light", FakeProvider())
    assert out["verdict"]["status"] == "unsupported"
    assert "No information found" in out["verdict"]["text"]
    assert out["dispatch"]["action"] == "roadside_rescue"


def test_the_neutral_fake_note_is_shown_for_every_customer_and_the_dispatch_line_comes_from_the_tool():
    for customer in ("C-1001", "C-1002", "C-1003"):
        for text in (RED_BRAKE, "check engine light", "ESC light is flashing", "coolant light and check engine light both on"):
            out = run(customer, text, FakeProvider())
            assert out["note"] == "I looked up the customer, the car and the company procedure."
            assert out["reply"].startswith(agent.NOTE_LABEL + out["note"])
            dispatch = desk.plan_dispatch(customer, text)
            assert out["dispatch"] == dispatch
            line = out["reply"].splitlines()[-1]
            assert line == agent.dispatch_line(dispatch)
            assert agent.ACTION_LABELS[dispatch["action"]] in line
            assert (dispatch["provider"] is None) or (f"via {dispatch['provider']}" in line)
            assert ("covered by the plan" in line) == dispatch["covered"]


def test_the_dispatch_line_ignores_what_the_model_says_about_dispatch():
    out = run("C-1001", RED_BRAKE, Scripted(Turn("I booked a dealer appointment, no need for a tow.")))
    assert out["note"] == agent.WITHHELD_NOTE
    assert out["reply"].endswith(agent.dispatch_line(desk.plan_dispatch("C-1001", RED_BRAKE)))
    assert "tow to the dealer via Hollow Valley Towing" in out["reply"]
    assert "appointment" not in out["reply"]


def test_a_failed_dispatch_tool_gets_a_plain_dispatch_line():
    assert agent.dispatch_line({"error": "tool_failed"}) == "carmind dispatch: not available, ask a supervisor."


@pytest.mark.parametrize("text", [
    "I booked roadside rescue; no need to pull over.",
    "carry on to the destination",
    "The recovery truck is on its way",
    "A dealer will see you",
    "Your appointment is set",
    "Dispatch has been sent",
    "Roadside help is coming",
])
def test_dispatch_wording_in_the_model_note_is_withheld(text):
    out = run("C-1001", RED_BRAKE, Scripted(Turn(text)))
    assert out["note"] == agent.WITHHELD_NOTE
    assert text not in out["reply"]


def test_summaries_are_short():
    out = run("C-1001", RED_BRAKE, FakeProvider())
    assert all(len(c["summary"]) <= 120 for c in out["tool_calls"])
    assert out["tool_calls"][1]["summary"] == "not_drivable, brake_fluid_low severity stop_now"


# The withhold list sees through invisible, look-alike and non-Latin disguises
def _fullwidth(text):
    return "".join(chr(ord(c) + 0xFEE0) for c in text)


@pytest.mark.parametrize("text", [
    "הרכב תקין לנסיעה.",
    "dr" + chr(0x200B) + "ivable",
    "s" + chr(0x00AD) + "afe",
    chr(0x0455) + "afe",
    _fullwidth("drivable"),
    "tow" + chr(0x2060) + "ing",
    "caf" + chr(0x00E9) + " visit",
])
def test_disguised_wording_is_withheld(text):
    assert agent.guard_note(text) == agent.WITHHELD_NOTE


def test_the_hebrew_clearance_label_from_the_issue_is_withheld_in_a_reply():
    text = "הרכב תקין לנסיעה."
    out = run("C-1001", RED_BRAKE, Scripted(Turn(text)))
    assert out["note"] == agent.WITHHELD_NOTE
    assert text not in out["reply"]


def test_every_stress_driving_clearance_and_the_hebrew_stems_are_withheld():
    import stress
    for phrase in stress.DRIVING_CLEARANCES:
        assert agent.guard_note(phrase) == agent.WITHHELD_NOTE, phrase
    for word in ("לנסוע", "נסיעה",
                 "תקין", "גרירה"):
        assert agent.guard_note("x " + word + " y") == agent.WITHHELD_NOTE, word


@pytest.mark.parametrize("word", [
    "قيادة",      # driving
    "آمنة",            # safe
    "سحب",                   # tow
    "السيارة لا تقود",
])
def test_arabic_drive_safe_tow_wording_is_withheld(word):
    assert agent.guard_note("x " + word + " y") == agent.WITHHELD_NOTE


def test_vowel_marks_do_not_hide_a_hebrew_stem():
    marked = "לִנְסוֹעַ"
    assert agent.guard_note(marked) == agent.WITHHELD_NOTE


def test_plain_hebrew_and_arabic_notes_without_a_stem_are_shown():
    hebrew = "הלקוח עודכן."
    arabic = "تم إبلاغ العميل."
    assert agent.guard_note(hebrew) == hebrew
    assert agent.guard_note(arabic) == arabic
    assert agent.guard_note("I have notified the customer.") == "I have notified the customer."


_ALL_VERDICTS = ("stop_now", "conditional", "caution", "info", "tow", "escalate", None, "unknown")
_ALL_LANGUAGES = ("he-IL", "en-US", "ar-SA", "ru-RU")


def _rendered_labels():
    labels = {compose.verdict_label(v, lang) for v in _ALL_VERDICTS for lang in _ALL_LANGUAGES}
    keys = ("do_not_drive", "conditional", "caution", "operational", "escalate_verdict", "tow")
    labels |= {compose._labels(lang)[k] for lang in _ALL_LANGUAGES for k in keys}
    assert len(labels) == 12  # 6 Hebrew + 6 English
    return labels


def test_every_compose_verdict_label_is_withheld():
    for label in sorted(_rendered_labels()):
        for text in (label, "x " + label + " y", label.rstrip("."), label.upper()):
            assert agent.guard_note(text) == agent.WITHHELD_NOTE, label


def test_a_neutral_note_is_still_shown_next_to_the_label_check():
    assert agent.guard_note(providers.FAKE_NOTE) == providers.FAKE_NOTE
    assert agent.guard_note("I have notified the customer.") == "I have notified the customer."


def test_the_hebrew_escalate_label_is_withheld_in_a_reply():
    text = compose.verdict_label("escalate", "he-IL")
    out = run("C-1001", RED_BRAKE, Scripted(Turn(text)))
    assert out["note"] == agent.WITHHELD_NOTE
    assert text not in out["reply"]


def test_verdict_labels_with_shuffled_case_and_spacing_are_withheld():
    rng = random.Random(0)
    labels = sorted(_rendered_labels())
    separators = (" ", "  ", "\n", "\t ", " \n ")
    wrappers = ("Note:", "fyi", "")
    for _ in range(500):
        label = rng.choice(labels)
        joined = rng.choice(separators).join(label.split())
        cased = "".join(rng.choice((c.upper(), c.lower())) if c.isascii() else c for c in joined)
        text = " ".join(part for part in (rng.choice(wrappers), cased, rng.choice(wrappers)) if part)
        assert agent.guard_note(text) == agent.WITHHELD_NOTE, repr(text)


# Any failure of the turn takes the deterministic fallback
def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_dead_mcp_service_still_gets_the_deterministic_stop_verdict():
    started = time.monotonic()
    tools = agent.McpTools(f"http://127.0.0.1:{_closed_port()}/mcp")
    out = run("C-1001", RED_BRAKE, FakeProvider(), tools=tools)
    assert time.monotonic() - started < agent.AGENT_DEADLINE_S
    assert out["error"] is not None and "internal" not in out["error"]
    assert out["verdict"]["status"] == "not_drivable"
    assert out["verdict"]["severity"] == "stop_now"
    assert "Do not drive." in out["reply"]
    assert out["reply"] == tail("C-1001", RED_BRAKE)
    assert out["dispatch"]["action"] == "tow_to_dealer"
    assert out["note"] == ""


def test_an_exception_group_from_the_turn_takes_the_fallback():
    class Grouped:
        @asynccontextmanager
        async def connect(self):
            raise ExceptionGroup("wrapped", [ConnectionError("down")])
            yield  # pragma: no cover

    out = run("C-1001", RED_BRAKE, FakeProvider(), tools=Grouped())
    assert out["verdict"]["status"] == "not_drivable"
    assert out["reply"] == tail("C-1001", RED_BRAKE)


def test_a_guarded_tool_that_errors_over_the_transport_is_re_run_in_process():
    class Broken:
        @asynccontextmanager
        async def connect(self):
            async def call(name, args):
                raise ConnectionError("transport down")
            yield agent.ToolBox([], call)

    out = run("C-1001", RED_BRAKE, Scripted(Turn("", [ToolCall("a", "get_customer", {})]), Turn("Done.")), tools=Broken())
    assert out["verdict"]["status"] == "not_drivable"
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))
    assert out["customer"]["id"] == "C-1001"


@pytest.mark.parametrize("args", ["oops", ["a"], None, 5, True])
def test_non_dict_tool_args_are_a_tool_error_and_do_not_raise(args):
    out = run("C-1001", RED_BRAKE, Scripted(Turn("", [ToolCall("a", "assess_warning_light", args)]), Turn("Done.")))
    assert out["tool_calls"][0] == {"name": "assess_warning_light", "by": "model", "summary": "error: bad_arguments"}
    assert out["tool_calls"][1]["by"] == "guard"
    assert out["verdict"]["status"] == "not_drivable"
    assert out["error"] is None


def test_non_dict_tool_args_from_a_provider_do_not_500_the_route(client, monkeypatch):
    provider = Scripted(Turn("", [ToolCall("a", "get_customer", "oops")]), Turn("Done."))
    monkeypatch.setattr(providers, "get_provider", lambda: provider)
    resp = client.post("/api/agent", json={"customer_id": "C-1001", "problem": RED_BRAKE})
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] is None
    assert data["verdict"]["status"] == "not_drivable"
    assert "Do not drive." in data["reply"]


# The deadline covers the whole turn
@pytest.fixture
def short_deadline(monkeypatch):
    monkeypatch.setattr(agent, "AGENT_DEADLINE_S", 0.5)


def _assert_fallback(out, started):
    assert time.monotonic() - started < 0.5 + 2
    assert out["error"] == "the agent did not finish in time."
    assert out["note"] == ""
    assert out["verdict"]["status"] == "not_drivable"
    assert out["verdict"]["text"] == truth("C-1001", RED_BRAKE)
    assert out["reply"] == tail("C-1001", RED_BRAKE)
    assert out["dispatch"]["action"] == "tow_to_dealer"


def test_a_stalled_provider_returns_the_fallback_within_the_deadline(short_deadline):
    class Stalled:
        name = "stalled"

        async def complete(self, *a):
            await asyncio.sleep(3600)

    started = time.monotonic()
    _assert_fallback(run("C-1001", RED_BRAKE, Stalled()), started)


def test_a_stalled_mcp_call_returns_the_fallback_within_the_deadline(short_deadline):
    class StalledTools:
        @asynccontextmanager
        async def connect(self):
            async def call(name, args):
                await asyncio.sleep(3600)
            yield agent.ToolBox([], call)

    provider = Scripted(Turn("", [ToolCall("a", "get_customer", {})]), Turn("Done."))
    started = time.monotonic()
    _assert_fallback(run("C-1001", RED_BRAKE, provider, tools=StalledTools()), started)


def test_a_stalled_mcp_connection_returns_the_fallback_within_the_deadline(short_deadline):
    class StalledConnect:
        @asynccontextmanager
        async def connect(self):
            await asyncio.sleep(3600)
            yield agent.ToolBox([], None)

    started = time.monotonic()
    _assert_fallback(run("C-1001", RED_BRAKE, FakeProvider(), tools=StalledConnect()), started)


# Routes
def test_index_page_is_ascii_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert 'id="ask"' in resp.text
    assert resp.content.isascii()


def test_customers_route_lists_four(client):
    customers = client.get("/api/customers").json()["customers"]
    assert len(customers) == 4
    assert set(customers[0]) == {"id", "name", "phone", "vehicles", "coverage", "location", "zone"}


def test_agent_route_returns_all_keys(client, monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")
    resp = client.post("/api/agent", json={"customer_id": "C-1001", "problem": RED_BRAKE})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == KEYS
    assert data["verdict"]["status"] == "not_drivable"
    assert data["provider"] == "fake"
    assert data["choice"] is None and data["context"] is None


@pytest.mark.parametrize("body", [
    {"customer_id": "C-9999", "problem": RED_BRAKE},
    {"customer_id": "C-1001"},
    {"customer_id": "C-1001", "problem": "B" * 4001},
    {"customer_id": "C-1001", "problem": ""},
])
def test_agent_route_rejects_bad_requests(client, body):
    resp = client.post("/api/agent", json=body)
    assert resp.status_code == 400
    assert set(resp.json()) == ({"error", "precaution"} if body["customer_id"] == "C-9999" else {"error"})


def test_agent_route_rejects_a_body_that_is_not_json(client):
    assert client.post("/api/agent", content=b"not json").status_code == 400


def test_a_real_provider_without_a_key_still_gets_the_verdict(client, monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.post("/api/agent", json={"customer_id": "C-1001", "problem": RED_BRAKE})
    assert resp.status_code == 200
    data = resp.json()
    assert "ANTHROPIC_API_KEY" in data["error"]
    assert data["verdict"]["status"] == "not_drivable"


def test_contract_route_still_answers(client):
    resp = client.post("/", json={"language": "en-US", "messages": [{"role": "user", "content": RED_BRAKE}],
                                       "plate": "FH-104-27"})
    assert resp.status_code == 200
    assert "Do not drive." in resp.json()["reply"]


def test_agent_route_takes_the_caller_number_and_plate(client, monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")
    data = client.post("/api/agent", json={"caller_number": "+1-555-0177", "plate": "FH-452-63",
                                           "problem": RED_BRAKE}).json()
    assert data["verdict"]["status"] == "not_drivable"
    assert data["customer"]["id"] == "C-1004"


def test_agent_route_without_customer_asks_for_the_plate(client, monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")
    data = client.post("/api/agent", json={"problem": RED_BRAKE}).json()
    assert data["verdict"]["status"] == "not_drivable"
    assert data["verdict"]["text"] == (compose.compose_escalate("en-US", "vehicle_unknown", text_route=False)
                                       + "\n\n" + compose.stop_precaution("en-US"))
    assert "error" in data["dispatch"]


def test_forced_args_reach_only_the_tools_that_take_them():
    class Box:
        def __init__(self):
            self.seen = []

        async def call(self, name, args):
            self.seen.append(dict(args))
            return {}

    forced = {"customer_id": "C-1001", "caller_number": "+1-555-0142", "plate": "FH-104-27",
              "description": "x", "after_hours": False, "extra": 1}
    box = Box()
    asyncio.run(agent._call(box, "assess_warning_light", {}, forced))
    asyncio.run(agent._call(box, "get_company_procedure", {}, forced))
    asyncio.run(agent._call(box, "assess_warning_light", {"customer_id": "C-9999"}, forced))
    FORCED = ("customer_id", "caller_number", "plate", "description", "after_hours")
    assessed, procedure, overridden = box.seen
    for key in FORCED:
        assert assessed[key] == forced[key]
    assert "extra" not in assessed
    assert overridden["customer_id"] == "C-1001"
    assert not set(FORCED) & set(procedure)


# luna on the agent route
LP_EN = "coolant warning came on after the engine control unit was updated"
LD_EN = "check engine light came on after I topped up the coolant"
LF_EN = "check engine light came on again"
BOTH = "coolant light and check engine light both on"


@pytest.fixture
def luna_on(monkeypatch):
    monkeypatch.setenv("LUNA_MODE", "on")
    monkeypatch.delenv("LUNA_TOTAL_BUDGET_S", raising=False)


class Chooser(FakeProvider):
    name = "scripted"

    def __init__(self, light=("ask", "r"), flags=([], "r"), turns=None):
        self.light, self.flags = light, flags
        self.turns = list(turns) if turns is not None else None
        self.requests = []

    @staticmethod
    def _play(answer, request):
        if isinstance(answer, BaseException):
            raise answer
        return answer(request) if callable(answer) else answer

    async def choose(self, request):
        self.requests.append(request)
        return self._play(self.light, request)

    async def choose_many(self, request):
        self.requests.append(request)
        return self._play(self.flags, request)

    async def complete(self, system, messages, tools):
        if self.turns is None:
            return await super().complete(system, messages, tools)
        turn = self.turns.pop(0) if self.turns else Turn("")
        if isinstance(turn, BaseException):
            raise turn
        return turn


def _verdict_of(assessed):
    return {"status": assessed["status"], "severity": assessed["severity"], "indicator": assessed["indicator"],
            "text": assessed["verdict_text"], "source": assessed["source"]}


@pytest.mark.usefixtures("luna_on")
def test_luna_picks_before_the_turn_and_the_tools_follow():
    out = run("C-1002", LP_EN, FakeProvider())
    assert out["choice"]["status"] == "picked" and out["choice"]["picked"] == "coolant_level_low"
    assert out["verdict"] == _verdict_of(desk.assess_for_agent("C-1002", LP_EN, False, "", "", "coolant_level_low", []))
    assert out["dispatch"] == desk.dispatch_for_agent("C-1002", LP_EN, False, "", "", "coolant_level_low", [])


@pytest.mark.usefixtures("luna_on")
def test_dispatch_is_the_more_conservative_plan():
    out = run("C-1001", LD_EN, FakeProvider())
    assert (out["verdict"]["status"], out["verdict"]["indicator"]) == ("drivable_caution", "engine_control_petrol")
    dispatch = out["dispatch"]
    assert (dispatch["action"], dispatch["covered"], dispatch["status"]) == ("roadside_rescue", True, "unclear")


@pytest.mark.usefixtures("luna_on")
def test_inferred_flags_tighten_and_are_shown():
    out = run("C-1002", LF_EN, FakeProvider())
    assert out["context"]["status"] == "chosen" and out["context"]["flags"] == ["repeat_problem"]
    assert out["verdict"]["status"] == "not_drivable"
    assert "Tow recommended - do not drive." in out["verdict"]["text"]
    assert out["dispatch"]["action"] == "tow_to_dealer"
    assert compose.agent_flags_line(["repeat_problem"], providers.FAKE_FLAGS_REASON, False) in out["reply"]


@pytest.mark.usefixtures("luna_on")
def test_a_failed_turn_keeps_the_inferred_flags():
    provider = Chooser(flags=(["repeat_problem"], "r"), turns=[ProviderError("scripted returned HTTP 500")])
    out = run("C-1002", LF_EN, provider)
    assert out["context"]["flags"] == ["repeat_problem"]
    assert out["verdict"]["status"] == "not_drivable"
    assert "Tow recommended - do not drive." in out["verdict"]["text"]
    assert out["choice"] is None


@pytest.mark.usefixtures("luna_on")
def test_inferred_flags_that_fire_a_rule_keep_the_light_choice_out():
    class RepeatChooser(FakeProvider):
        def __init__(self):
            self.choose_calls = []

        async def choose(self, request):
            self.choose_calls.append(request)
            return request.options[0].id, "r"

        async def choose_many(self, request):
            return ["repeat_problem"], "r"

    provider = RepeatChooser()
    out = run("C-1002", "coolant light and check engine light both on again", provider)
    assert provider.choose_calls == []
    assert out["choice"] is None
    assert out["verdict"]["status"] == "not_drivable"
    assert "Light picked" not in out["reply"]


@pytest.mark.usefixtures("luna_on")
def test_the_model_cannot_set_the_light():
    provider = Chooser(light=("ask", "r"), turns=[
        Turn("", [ToolCall("a", "assess_warning_light", {"light": "coolant_level_low"})]), Turn("Done.")])
    out = run("C-1001", BOTH, provider)
    assert out["choice"]["status"] == "asked"
    assert out["tool_calls"][0]["name"] == "assess_warning_light" and out["tool_calls"][0]["by"] == "model"
    assert out["verdict"]["status"] == "unclear"


@pytest.mark.usefixtures("luna_on")
@pytest.mark.parametrize("text", [RED_BRAKE, "check engine light", "brake fluid light and coolant light both on"])
def test_no_choice_for_a_stop_light_or_a_single_light(monkeypatch, text):
    calls = []

    async def recorder(*a, **k):
        calls.append(a)

    monkeypatch.setattr(luna, "choose", recorder)
    out = run("C-1001", text, FakeProvider())
    assert calls == []
    assert out["choice"] is None


@pytest.mark.usefixtures("luna_on")
@pytest.mark.parametrize("light,status", [(ProviderError("down"), "failed"), (("esc_active", "r"), "rejected")])
def test_failed_and_rejected_choices_leave_the_unclear_verdict(light, status):
    out = run("C-1001", BOTH, Chooser(light=light))
    assert out["choice"]["status"] == status
    assert out["verdict"]["status"] == "unclear"
    assert out["dispatch"] == desk.plan_dispatch("C-1001", BOTH)
    assert "Light picked" not in out["reply"]


@pytest.mark.usefixtures("luna_on")
def test_withheld_reasoning():
    out = run("C-1001", BOTH, Chooser(light=lambda r: (r.options[0].id, "safe to drive now")))
    assert out["choice"]["status"] == "picked"
    assert out["choice"]["withheld"] is True and out["choice"]["reasoning"] == ""
    assert "withheld by carmind's word and character filter." in out["reply"]
    assert "safe to drive" not in json.dumps(out).lower()


@pytest.mark.usefixtures("luna_on")
def test_no_provider_skips_the_choice():
    out = run("C-1002", LP_EN, None, setup_error="no provider")
    assert out["choice"] is None and out["context"] is None
    assert out["verdict"]["status"] == "unclear"


@pytest.mark.usefixtures("luna_on")
def test_luna_gets_no_customer_data():
    provider = Chooser()
    # Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
    problem = LP_EN + " call me on 555.0142 or qzxvw@qzxvw.example.com, plate FH-104-27"
    asyncio.run(agent.run_agent("C-1001", problem, False, provider, agent.LocalTools(),
                                caller_number="+1-555-0142", plate="FH-104-27"))
    assert len(provider.requests) == 2
    for request in provider.requests:
        sent = " ".join([request.question, request.context]
                        + [f"{o.id} {o.name} {o.text} {' '.join(o.words)}" for o in request.options])
        assert not any(c.isdigit() for c in sent), sent
        for piece in ("@", "FH", "qz", "Doe"):
            assert piece not in sent, (piece, sent)


def _provider_error_turns(after_tool_results):
    error = ProviderError("scripted returned HTTP 500")
    if after_tool_results:
        return [Turn("", [ToolCall("a", "assess_warning_light", {})]), error]
    return [error]


def _assert_the_pick_was_dropped(out):
    assert out["choice"] is None
    assert out["verdict"]["status"] == "unclear"
    assert out["note"].startswith("The language model is not available:")
    assert "Light picked" not in out["reply"]


@pytest.mark.usefixtures("luna_on")
def test_provider_error_after_a_pick_before_tool_results():
    provider = Chooser(light=("coolant_level_low", "r"), turns=_provider_error_turns(False))
    _assert_the_pick_was_dropped(run("C-1001", BOTH, provider))


@pytest.mark.usefixtures("luna_on")
def test_provider_error_after_a_pick_after_tool_results():
    provider = Chooser(light=("coolant_level_low", "r"), turns=_provider_error_turns(True))
    out = run("C-1001", BOTH, provider)
    assert out["tool_calls"][0]["by"] == "model" and out["tool_calls"][0]["summary"].startswith("drivable_caution")
    _assert_the_pick_was_dropped(out)


@pytest.mark.usefixtures("luna_on")
@pytest.mark.parametrize("failure", ["timeout", "dead_mcp"])
def test_failed_turn_after_ask_drops_the_choice(monkeypatch, failure):
    if failure == "timeout":
        monkeypatch.setattr(agent, "AGENT_DEADLINE_S", 0.5)

        class Stalled(Chooser):
            async def complete(self, *a):
                await asyncio.sleep(3600)

        provider, tools = Stalled(light=("ask", "r")), None
    else:
        provider, tools = Chooser(light=("ask", "r")), agent.McpTools(f"http://127.0.0.1:{_closed_port()}/mcp")
    out = run("C-1001", BOTH, provider, tools=tools)
    assert len(provider.requests) == 2
    assert out["choice"] is None
    assert out["verdict"]["status"] == "unclear"
    assert out["note"] == ""


@pytest.mark.usefixtures("luna_on")
def test_concurrent_turns_with_the_choice():
    provider = FakeProvider()
    expected = {LP_EN: "picked", LD_EN: "picked", BOTH: "asked", RED_BRAKE: None}
    cases = [(c, t) for c in ("C-1001", "C-1002") for t in expected] * 3

    async def go():
        return await asyncio.gather(*[agent.run_agent(c, t, False, provider, agent.LocalTools()) for c, t in cases])

    results = asyncio.run(go())
    assert len(results) == 24
    for out, (customer, text) in zip(results, cases):
        assert (out["choice"] or {}).get("status") == expected[text], (customer, text, out["choice"])


# The Anthropic provider in the agent loop: thinking blocks go back with the tool results
ANTHROPIC_NOTE = "I looked up the customer and the car."


def _thinking(sig):
    return {"type": "thinking", "thinking": "plan the lookups", "signature": sig}


def _tool_turn(sig):
    calls = [{"type": "tool_use", "id": f"t{i}", "name": name, "input": {}} for i, name in enumerate(TOOL_NAMES, 1)]
    return {"content": [_thinking(sig), *calls], "stop_reason": "tool_use"}


def _has_tool_result(body):
    return any(isinstance(m["content"], list) and any(b.get("type") == "tool_result" for b in m["content"])
               for m in body["messages"])


def _anthropic(handler):
    return providers.AnthropicProvider("k", "test-model", transport=httpx.MockTransport(handler))


def test_anthropic_agent_loop_replays_thinking_and_shows_the_note(monkeypatch):
    monkeypatch.setenv("LUNA_MODE", "off")
    seen = []
    first = _tool_turn("sig-a")

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if _has_tool_result(body):
            payload = {"content": [_thinking("sig-b"), {"type": "text", "text": ANTHROPIC_NOTE}], "stop_reason": "end_turn"}
        else:
            payload = first
        return httpx.Response(200, json=payload)

    out = run("C-1001", RED_BRAKE, _anthropic(handler))
    assert len(seen) == 2
    assert seen[1]["messages"][0] == seen[0]["messages"][0]
    assert seen[1]["messages"][1] == {"role": "assistant", "content": first["content"]}
    assert [b["tool_use_id"] for b in seen[1]["messages"][2]["content"]] == ["t1", "t2", "t3"]
    assert out["error"] is None
    assert out["note"] == ANTHROPIC_NOTE
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))
    assert [(c["name"], c["by"]) for c in out["tool_calls"]] == [(n, "model") for n in TOOL_NAMES]


REFUSAL = {"content": [{"type": "text", "text": "I can't help with that."}], "stop_reason": "refusal"}


@pytest.mark.parametrize("first,second", [
    (REFUSAL, None),
    ({"content": [_thinking("sig-a")], "stop_reason": "max_tokens"}, None),
    (_tool_turn("sig-a"), REFUSAL),
], ids=["refusal_first", "cap_first", "refusal_after_the_tool_turn"])
def test_anthropic_refusal_or_cap_takes_the_safe_path(monkeypatch, first, second):
    monkeypatch.setenv("LUNA_MODE", "off")

    def handler(request):
        return httpx.Response(200, json=second if _has_tool_result(json.loads(request.content)) else first)

    out = run("C-1001", RED_BRAKE, _anthropic(handler))
    assert out["error"] == "unexpected response from anthropic"
    assert out["note"].startswith("The language model is not available:")
    assert out["verdict"]["status"] == "not_drivable"
    assert out["reply"].endswith(tail("C-1001", RED_BRAKE))
    if second is None:
        assert [c["by"] for c in out["tool_calls"]] == ["guard"] * 3


def test_concurrent_anthropic_agent_turns_replay_their_own_thinking(monkeypatch):
    monkeypatch.setenv("LUNA_MODE", "off")
    cases = [(c, p, ah) for c in ("C-1001", "C-1002", "C-1003", "C-1004") for ah in (False, True)
             for p in (RED_BRAKE, "check engine light")]
    sigs, replayed = {}, {}

    def handler(request):
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        if not _has_tool_result(body):
            sig = sigs.setdefault(prompt, "sig-" + chr(ord("a") + len(sigs)))
            return httpx.Response(200, json=_tool_turn(sig))
        replayed[prompt] = [b["signature"] for b in body["messages"][1]["content"] if b["type"] == "thinking"]
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn"})

    provider = _anthropic(handler)

    async def go():
        return await asyncio.gather(*[agent.run_agent(c, p, ah, provider, agent.LocalTools()) for c, p, ah in cases])

    results = asyncio.run(go())
    assert len(results) == len(sigs) == len(replayed) == 16
    assert all(replayed[prompt] == [sig] for prompt, sig in sigs.items())
    for out, (customer, problem, after_hours) in zip(results, cases):
        assert out["error"] is None
        assert out["verdict"]["status"] == desk.assess_warning_light(customer, problem, after_hours)["status"]
