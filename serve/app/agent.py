"""
The call-desk agent: a language model may call the four desk tools, but the verdict never comes from it.

The tool results the reply is built from are always the deterministic ones: the three
tools the screen needs are re-run by the guard when the model skipped them or they failed,
and customer id, caller number, plate number, description and after-hours are forced onto
every tool call that takes them. The model's text
is shown only as a separately labelled note, and is withheld entirely when it touches on
the verdict, drivability, severity or dispatch; the dispatch line of the reply is built from the
plan_dispatch tool result. The whole turn runs under one deadline, and any failure of the turn
(the deadline included) falls back to the deterministic assessment, run in-process, with no model note.
With LUNA_MODE=on, luna may first infer context flags, which only tighten, and pick one of several matched
drivable lights, which is forced onto assess_warning_light; the assessment and dispatch the reply shows are then
computed in-process with them.
"""
import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from app import desk, luna, mcp_server
from app.compose import (
    agent_choice_line,
    agent_flags_line,
    compose_escalate,
    stop_precaution,
)
from app.noteguard import WITHHELD_NOTE, guard_note
from app.orchestrator import FLAGS_QUESTION, LIGHT_QUESTION, stop_warning_or_fallback
from app.providers import ProviderError

logger = logging.getLogger(__name__)

MAX_MODEL_TURNS = 5
MAX_CALLS_PER_TURN = 6
AGENT_DEADLINE_S = 60.0
MCP_CALL_TIMEOUT_S = 10.0
GUARDED_TOOLS = ("get_customer", "assess_warning_light", "plan_dispatch")

NOTE_LABEL = "Model note (not a verdict): "
VERDICT_LABEL = "carmind verdict: "
DISPATCH_LABEL = "carmind dispatch: "
ACTION_LABELS = {
    "none": "no dispatch needed",
    "roadside_rescue": "roadside rescue",
    "tow_to_dealer": "tow to the dealer",
    "dealer_appointment": "dealer appointment",
}
UNKNOWN_TOOL = "unknown_tool"
SYSTEM_PROMPT = (
    "You help a call-center operator of a car rental company. Use the tools to look up the customer, "
    "assess the warning light and plan the dispatch. Never say whether the car is safe or drivable: "
    "the system adds the carmind verdict after your text. Write two or three short sentences in English "
    "for the operator: what to tell the customer and what happens next."
)


@dataclass
class ToolBox:
    specs: list[dict]
    call: Callable[[str, dict], Awaitable[dict]]


class LocalTools:
    """The desk tools called in-process (tests, and the fallback when the deadline expires)."""

    @staticmethod
    async def call(name: str, args: dict) -> dict:
        return desk.TOOLS[name](**args)

    @asynccontextmanager
    async def connect(self):
        listed = await mcp_server.server.list_tools()
        specs = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in listed]
        yield ToolBox(specs, self.call)


class McpTools:
    """The desk tools called over streamable HTTP on the separate MCP service."""

    def __init__(self, url: str):
        self.url = url

    @asynccontextmanager
    async def connect(self):
        async with streamablehttp_client(self.url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), MCP_CALL_TIMEOUT_S)
                listed = await asyncio.wait_for(session.list_tools(), MCP_CALL_TIMEOUT_S)
                specs = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                         for t in listed.tools]

                async def call(name: str, args: dict) -> dict:
                    result = await asyncio.wait_for(session.call_tool(name, args), MCP_CALL_TIMEOUT_S)
                    text = result.content[0].text if result.content else ""
                    if result.isError:
                        return {"error": text[:200]}
                    return json.loads(text)

                yield ToolBox(specs, call)


LOCAL_BOX = ToolBox([], LocalTools.call)


def tools_from_env():
    url = os.environ.get("CARMIND_MCP_URL")
    return McpTools(url) if url else LocalTools()


async def _call(box: ToolBox, name: str, args, forced: dict) -> dict:
    """Run one tool with the forced arguments laid over the model's."""
    if not isinstance(name, str) or name not in desk.TOOLS:
        return {"error": UNKNOWN_TOOL}
    if not isinstance(args, dict):
        return {"error": "bad_arguments"}
    params = inspect.signature(desk.TOOLS[name]).parameters
    clean = {k: v for k, v in args.items() if k in params}
    clean.update({k: v for k, v in forced.items() if k in params})
    try:
        result = await box.call(name, clean)
    except Exception as exc:
        logger.error("tool %s failed: %s", name, type(exc).__name__)
        return {"error": "tool_failed"}
    return result if isinstance(result, dict) else {"error": "tool_failed"}


def summarize(name: str, result: dict) -> str:
    """A short, deterministic line about a tool result, for the tool-call log."""
    if "error" in result:
        return f"error: {result['error']}"[:120]
    if name == "get_customer":
        text = f"{result['name']}, {'; '.join(desk.car_label(v) for v in result['vehicles'])}, coverage {result['coverage']}"
    elif name == "assess_warning_light":
        text = result["status"]
        if result.get("indicator"):
            text += f", {result['indicator']} severity {result['severity']}"
    elif name == "get_company_procedure":
        text = f"{result['situation']}: {len(result['steps'])} steps"
    elif name == "plan_dispatch":
        text = result["action"]
        if result.get("provider"):
            text += f" via {result['provider']}"
        text += ", covered" if result["covered"] else ", not covered"
    else:
        text = ""
    return text[:120]


def dispatch_line(dispatch: dict) -> str:
    """The dispatch statement of the reply, built only from the plan_dispatch tool's fields."""
    if "error" in dispatch:
        return DISPATCH_LABEL + "not available, ask a supervisor."
    text = ACTION_LABELS.get(dispatch.get("action"), "follow the company procedure")
    if dispatch.get("provider"):
        text += f" via {dispatch['provider']}"
    text += ", covered by the plan" if dispatch.get("covered") else ", not covered, the customer pays"
    return DISPATCH_LABEL + text + "."


def _for_model(name: str, result: dict) -> dict:
    """A tool result as the model sees it: the customer's phone number is left out."""
    if name == "get_customer" and "phone" in result:
        return {k: v for k, v in result.items() if k != "phone"}
    return result


@dataclass
class _Run:
    results: dict = field(default_factory=dict)
    tool_calls: list = field(default_factory=list)
    note: str = ""
    error: str | None = None
    choice: dict | None = None
    context: dict | None = None
    inferred: list = field(default_factory=list)


async def _model_loop(run: _Run, provider, box: ToolBox, user_prompt: str, forced: dict) -> None:
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    try:
        for _ in range(MAX_MODEL_TURNS):
            turn = await provider.complete(SYSTEM_PROMPT, messages, box.specs)
            run.note = turn.text
            if not turn.tool_calls:
                return
            messages.append({"role": "assistant", "text": turn.text, "tool_calls": list(turn.tool_calls),
                             "replay": getattr(turn, "replay", None)})
            for index, call in enumerate(turn.tool_calls):
                if index >= MAX_CALLS_PER_TURN:
                    result = {"error": "too_many_calls"}
                else:
                    # A name the model made up is never echoed: it is recorded as unknown_tool.
                    known = isinstance(call.name, str) and call.name in desk.TOOLS
                    name = call.name if known else UNKNOWN_TOOL
                    result = await _call(box, name, call.args, forced)
                    if known:
                        run.results[name] = result
                    run.tool_calls.append({"name": name, "by": "model", "summary": summarize(name, result)})
                messages.append({"role": "tool", "call_id": call.id, "name": call.name, "content": json.dumps(_for_model(name, result))})
    except ProviderError as exc:
        run.error = str(exc)


async def _guard(run: _Run, box: ToolBox, forced: dict) -> None:
    """Re-run, with the forced arguments, every screen tool the model skipped or that failed.

    A guarded tool that still errors is re-run in-process, so the verdict never depends on the MCP transport.
    """
    for name in GUARDED_TOOLS:
        if name not in run.results or "error" in run.results[name]:
            result = await _call(box, name, {}, forced)
            if "error" in result:
                result = await _call(LOCAL_BOX, name, {}, forced)
            run.results[name] = result
            run.tool_calls.append({"name": name, "by": "guard", "summary": summarize(name, result)})


async def _infer_flags(run: _Run, provider, forced: dict, deadline: float) -> None:
    """Before the light choice: ask luna which listed car-history facts the problem states."""
    if provider is None or not luna.enabled():
        return
    try:
        found = desk.flags_choice(forced["customer_id"], forced["description"], forced["after_hours"],
                                  forced["caller_number"], forced["plate"])
    except Exception as exc:
        logger.error("context inference failed: %s", type(exc).__name__)
        return
    if found is None:
        return
    context, options = found
    outcome = await luna.choose_many(FLAGS_QUESTION, context, options, provider, deadline=deadline)
    if not isinstance(outcome, luna.ManyOutcome):
        return
    run.context = {"status": outcome.status, "flags": list(outcome.choices), "options": [o.id for o in options],
                   "reasoning": outcome.reasoning, "withheld": outcome.withheld}
    if outcome.status == "chosen":
        run.inferred = list(outcome.choices)


async def _choose_light(run: _Run, provider, forced: dict, deadline: float) -> None:
    """Before the model's turn: ask luna which matched light the problem describes; a pick is forced as light."""
    if provider is None or not luna.enabled():
        return
    try:
        found = desk.light_choice(forced["customer_id"], forced["description"], forced["after_hours"],
                                  forced["caller_number"], forced["plate"], run.inferred)
    except Exception as exc:
        logger.error("light choice failed: %s", type(exc).__name__)
        return
    if found is None:
        return
    snippet, options = found
    outcome = await luna.choose(LIGHT_QUESTION, snippet, options, provider, deadline=deadline)
    if not isinstance(outcome, luna.Outcome):
        return
    run.choice = {"status": outcome.status, "picked": outcome.choice if outcome.status == "picked" else None,
                  "options": [{"id": o.id, "name": o.name} for o in options],
                  "reasoning": outcome.reasoning, "withheld": outcome.withheld}
    if outcome.status == "picked":
        forced["light"] = outcome.choice


async def _abandon_choice(run: _Run, forced: dict) -> None:
    """After a provider error or a failed turn: no light choice and no light on the tools, and the assessment
    computed again in-process without it. Inferred context flags are kept: they can only tighten."""
    consulted = run.choice is not None or bool(forced.get("light"))
    run.choice = None
    forced["light"] = ""
    if not consulted:
        return
    result = await _call(LOCAL_BOX, "assess_warning_light", {}, forced)
    run.results["assess_warning_light"] = result
    run.tool_calls.append({"name": "assess_warning_light", "by": "guard",
                           "summary": summarize("assess_warning_light", result)})


def _finalize(run: _Run, forced: dict) -> None:
    """When luna gave flags or a pick: the assessment and dispatch the reply shows, computed in-process."""
    if not run.inferred and not forced["light"]:
        return
    args = (forced["customer_id"], forced["description"], forced["after_hours"], forced["caller_number"],
            forced["plate"], forced["light"], run.inferred)
    for name, compute in (("assess_warning_light", desk.assess_for_agent),
                          ("plan_dispatch", desk.dispatch_for_agent)):
        try:
            result = compute(*args)
        except Exception as exc:
            logger.error("%s with luna's answers failed: %s", name, type(exc).__name__)
            result = {"error": "tool_failed"}
        if not isinstance(result, dict):
            result = {"error": "tool_failed"}
        run.results[name] = result
        run.tool_calls.append({"name": name, "by": "guard", "summary": summarize(name, result)})


async def _turn(run: _Run, customer_id: str, problem: str, after_hours: bool, provider, tools, setup_error, forced: dict):
    user_prompt = (f"Customer id: {customer_id or 'not given'}. After hours: {'yes' if after_hours else 'no'}. "
                   f"The customer's problem: {problem}")
    async with tools.connect() as box:
        if provider is None:
            run.error = setup_error
        else:
            await _model_loop(run, provider, box, user_prompt, forced)
            if run.error:
                await _abandon_choice(run, forced)
        await _guard(run, box, forced)


def _verdict_fields(assessed: dict, problem: str, flags) -> dict:
    if "error" in assessed:
        text, status = compose_escalate("en-US", "error"), "unclear"
        if stop_warning_or_fallback(problem, flags):
            text, status = text + "\n\n" + stop_precaution("en-US"), "not_drivable"
        return {"status": status, "severity": None, "indicator": None, "text": text, "source": None}
    return {"status": assessed["status"], "severity": assessed["severity"], "indicator": assessed["indicator"],
            "text": assessed["verdict_text"], "source": assessed["source"]}


async def run_agent(customer_id: str, problem: str, after_hours: bool, provider, tools, setup_error=None,
                    caller_number="", plate="") -> dict:
    """Run the agent turn and return the screen payload; the verdict and dispatch are the tools' own."""
    forced = {"customer_id": customer_id or "", "caller_number": caller_number, "plate": plate,
              "description": problem, "after_hours": after_hours, "light": ""}
    run = _Run()
    fell_back = False
    deadline = asyncio.timeout(AGENT_DEADLINE_S)
    try:
        async with deadline:
            request_deadline = luna.request_deadline()
            await _infer_flags(run, provider, forced, request_deadline)
            await _choose_light(run, provider, forced, request_deadline)
            await _turn(run, customer_id, problem, after_hours, provider, tools, setup_error, forced)
    except (Exception, BaseExceptionGroup) as exc:
        # Any failure of the turn (the deadline, a dead MCP service, an anyio task-group wrapper, a bad
        # provider answer) takes the deterministic fallback: no model note, the tools run in-process.
        expired = deadline.expired()
        if isinstance(exc, BaseExceptionGroup) and not isinstance(exc, Exception) and not expired:
            raise  # a cancellation that did not come from our deadline
        fell_back = True
        timed_out = expired or isinstance(exc, TimeoutError)
        if not timed_out:
            logger.error("agent turn failed: %s", type(exc).__name__)
        run.error = "the agent did not finish in time." if timed_out else "the agent turn failed."
        await _abandon_choice(run, forced)
        await _guard(run, LOCAL_BOX, forced)
    _finalize(run, forced)

    if fell_back:
        note = ""
    elif run.error:
        note = f"The language model is not available: {run.error} The verdict and dispatch come from the carmind tools."
    else:
        note = guard_note(run.note)
    verdict = _verdict_fields(run.results["assess_warning_light"], problem, desk.request_flags(after_hours, run.inferred))
    dispatch = run.results["plan_dispatch"]

    # The model's own text is the only part labelled as a note; the verdict and dispatch lines are code.
    parts = []
    if note and (run.error or note == WITHHELD_NOTE):
        parts.append(note)
    elif note:
        parts.append(NOTE_LABEL + note)
    if run.context is not None and run.context["flags"]:
        parts.append(agent_flags_line(run.context["flags"], run.context["reasoning"], run.context["withheld"]))
    if run.choice is not None:
        c = run.choice
        line = agent_choice_line(c["status"], next((o["name"] for o in c["options"] if o["id"] == c["picked"]), None),
                                 [o["name"] for o in c["options"]], c["reasoning"], c["withheld"])
        if line:
            parts.append(line)
    parts.append(VERDICT_LABEL + verdict["text"])
    parts.append(dispatch_line(dispatch))
    return {
        "customer": run.results["get_customer"],
        "verdict": verdict,
        "dispatch": dispatch,
        "note": note,
        "reply": "\n\n".join(parts),
        "tool_calls": run.tool_calls,
        "provider": provider.name if provider else (os.environ.get("CARMIND_PROVIDER") or "fake"),
        "error": run.error,
        "choice": run.choice,
        "context": run.context,
    }
