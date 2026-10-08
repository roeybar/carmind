"""
Language-model providers for the agent route: a fake one (default, no network), Anthropic and OpenAI.

Messages are neutral: {"role": "user", "content": str}, {"role": "assistant", "text": str,
"tool_calls": [ToolCall], "replay": list | None} and {"role": "tool", "call_id": str, "name": str, "content": json_str}.
"replay" holds the provider's own content blocks for that turn (for Anthropic, thinking blocks included); the same
provider sends them back unchanged. None means the turn is rebuilt from text and tool_calls.
Model ids and keys come from the environment only. Neither a key nor a request body is
ever logged or put in an error message.
Providers also answer luna's constrained choices (see luna.py).
"""
import copy
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

import httpx

PROVIDER_TIMEOUT_S = 30.0
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# The fake provider's whole note. It says nothing about the verdict or the dispatch, so the guard shows it.
FAKE_NOTE = "I looked up the customer, the car and the company procedure."

# The situation the fake asks get_company_procedure for when plan_dispatch named none.
FAKE_FALLBACK_SITUATION = "unclear_description"

ASK = "ask"
CHOICE_TOOL, MANY_TOOL = "choose", "choose_many"
CHOICE_MAX_TOKENS = 4096
AGENT_MAX_TOKENS = 8192
AGENT_EFFORT = "medium"
CHOICE_EFFORT = "medium"
TOOL_ONLY_INSTRUCTION = "Answer only by calling the tool, never in plain text."
ANTHROPIC_TURN_STOPS = ("end_turn", "tool_use")
OPENAI_CHOICE_MAX_TOKENS = 1024
CHOICE_SYSTEM = ("You pick one option from a closed list for carmind, a warning-light lookup used on roadside-assistance "
                 "calls. Read the caller's words and decide which option they describe. Write the reason first: one or "
                 "two short sentences in English saying which of the caller's words point to your choice. Then give "
                 "the choice, exactly one of the allowed ids. Do not write about driving, safety, severity, towing or "
                 "what the caller should do; carmind adds its own verdict from its records. If the words do not point "
                 "to exactly one option, choose ask.")
CHOICE_TOOL_DESCRIPTION = "Record the reason, then the choice."
CHOICE_REASON_DESCRIPTION = "One or two short sentences in English: which of the caller's words point to the choice."
CHOICE_ID_DESCRIPTION = "One of the allowed ids."
FLAGS_SYSTEM = ("You read a caller's description for carmind, a warning-light lookup used on roadside-assistance calls, "
                "and say which of the listed facts about the car the description states. Write the reason first: one "
                "or two short sentences in English quoting the words that state each fact. Then give the list of "
                "chosen ids, which may be empty. Choose a fact only when the description states it; do not guess. Do "
                "not write about driving, safety, severity, towing or what the caller should do; carmind decides with "
                "its own rules.")
MANY_TOOL_DESCRIPTION = "Record the reason, then the chosen facts."
MANY_REASON_DESCRIPTION = "One or two short sentences in English: which of the caller's words state each chosen fact."
MANY_IDS_DESCRIPTION = "The ids of the facts the description states; empty when it states none."
FAKE_PICK_REASON = ("Fixed rule of the fake provider: only this light is named with a word for a warning light next "
                    "to it.")
FAKE_ASK_REASON = ("Fixed rule of the fake provider: more than one light, or none, is named with a word for a warning "
                   "light next to it, so the caller is asked.")
FAKE_FLAGS_REASON = "Fixed rule of the fake provider: the description says the problem happened again."
FAKE_NO_FLAGS_REASON = "Fixed rule of the fake provider: the description does not say the problem happened again."
_LIGHT_WORD_AFTER = r"\s+(?:light|lamp|warning|indicator)s?\b"
_LIGHT_WORD_BEFORE_HE = r"(?:נורת|נורה)\s+ה?"
_REPEAT_CUE_EN = re.compile(r"\b(?:again|came back|comes back|coming back|second time|third time)\b")
_REPEAT_CUE_HE = re.compile(r"(?<![\u05d0-\u05ea])(?:שוב|פעם שנייה|פעם שלישית)(?![\u05d0-\u05ea])")
_STRICT_LOCK = threading.Lock()
_STRICT_STATE: dict[str, str] = {}   # model id -> "ok" | "refused"
_STRICT_PROBE: set[str] = set()      # model ids with a first strict request in flight
_CHOICE_STATE: dict[str, str] = {}   # model id -> "forced" | "auto"
_CHOICE_PROBE: set[str] = set()      # model ids with a first forced request in flight


class ProviderError(Exception):
    pass


@dataclass(frozen=True)
class Option:
    id: str
    name: str
    text: str
    words: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChoiceRequest:
    question: str
    context: str
    options: tuple[Option, ...]


class _HTTPStatus(ProviderError):
    def __init__(self, name: str, status: int, mentions_strict: bool = False, mentions_tool_choice: bool = False):
        super().__init__(f"{name} returned HTTP {status}")
        self.status, self.mentions_strict, self.mentions_tool_choice = status, mentions_strict, mentions_tool_choice


def _mentions_strict(resp) -> bool:
    """True when an error response's message names strict mode; the message itself is never kept."""
    try:
        message = resp.json()["error"]["message"]
    except Exception:
        return False
    return isinstance(message, str) and "strict" in message.lower()


def _mentions_tool_choice(resp) -> bool:
    """True when an error response's message names tool_choice; the message itself is never kept."""
    try:
        message = resp.json()["error"]["message"]
    except Exception:
        return False
    return isinstance(message, str) and "tool_choice" in message.lower()


def choice_schema(option_ids: list[str]) -> dict:
    return {"type": "object",
            "properties": {"reason": {"type": "string", "description": CHOICE_REASON_DESCRIPTION},
                           "choice": {"type": "string", "enum": [*option_ids, ASK],
                                      "description": CHOICE_ID_DESCRIPTION}},
            "required": ["reason", "choice"], "additionalProperties": False}


def many_schema(option_ids: list[str]) -> dict:
    return {"type": "object",
            "properties": {"reason": {"type": "string", "description": MANY_REASON_DESCRIPTION},
                           "choices": {"type": "array", "items": {"type": "string", "enum": list(option_ids)},
                                       "description": MANY_IDS_DESCRIPTION}},
            "required": ["reason", "choices"], "additionalProperties": False}


def choice_prompt(request: ChoiceRequest) -> str:
    lines = [request.question, "", f"The caller's words, masked: {request.context}", "", "Options:"]
    lines += [f"- {o.id}: {o.text} Matched words: {', '.join(o.words) or 'none'}." for o in request.options]
    lines.append(f"- {ASK}: the words do not point to exactly one of the options above, so carmind asks the caller.")
    return "\n".join(lines)


def many_prompt(request: ChoiceRequest) -> str:
    lines = [request.question, "", f"The caller's words, masked: {request.context}", "",
             "Facts you may choose (any number, or none):"]
    lines += [f"- {o.id}: {o.text}" for o in request.options]
    return "\n".join(lines)


def _named_as_light(option: Option, lower: str) -> bool:
    for word in option.words:
        escaped = re.escape(word.lower())
        if word.isascii():
            if re.search(r"(?<![a-z0-9])" + escaped + _LIGHT_WORD_AFTER, lower):
                return True
        elif re.search(_LIGHT_WORD_BEFORE_HE + escaped, lower):
            return True
    return False


def _answer_fields(name: str, answer, keys: tuple[str, str]) -> tuple:
    if not isinstance(answer, dict) or set(answer) != set(keys):
        raise ProviderError(f"unexpected response from {name}")
    return answer[keys[0]], answer[keys[1]]


def _strict_enabled() -> bool:
    return (os.environ.get("CARMIND_ANTHROPIC_STRICT") or "on").strip().lower() != "off"


def _set_strict(model: str, state: str) -> None:
    with _STRICT_LOCK:
        _STRICT_STATE[model] = state


def _set_choice(model: str, state: str) -> None:
    with _STRICT_LOCK:
        _CHOICE_STATE[model] = state


def _choice_body(body: dict, tool: dict, strict: bool, forced: bool) -> dict:
    """The constrained-choice request: the tool, strict or plain, forced or offered with tool_choice auto."""
    return {**body, "tools": [{**tool, "strict": True} if strict else tool],
            "tool_choice": {"type": "tool", "name": tool["name"]} if forced else {"type": "auto"}}


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    replay: list | None = None


class FakeProvider:
    """Calls the three screen tools, then get_company_procedure for the situation plan_dispatch named,
    then writes a neutral note. No network, no state."""

    name = "fake"

    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        results = {m["name"]: m["content"] for m in messages if m["role"] == "tool"}
        if not results:
            return Turn("", [ToolCall("fake-1", "get_customer", {}),
                             ToolCall("fake-2", "assess_warning_light", {}),
                             ToolCall("fake-3", "plan_dispatch", {})])
        if "get_company_procedure" not in results:
            situation = _fake_situation(results.get("plan_dispatch"))
            return Turn("", [ToolCall("fake-4", "get_company_procedure", {"situation": situation})])
        return Turn(FAKE_NOTE)

    async def choose(self, request: ChoiceRequest) -> tuple:
        lower = request.context.lower()
        named = [o.id for o in request.options if _named_as_light(o, lower)]
        return (named[0], FAKE_PICK_REASON) if len(named) == 1 else (ASK, FAKE_ASK_REASON)

    async def choose_many(self, request: ChoiceRequest) -> tuple:
        lower = request.context.lower()
        if "repeat_problem" in [o.id for o in request.options] and (
                _REPEAT_CUE_EN.search(lower) or _REPEAT_CUE_HE.search(lower)):
            return ["repeat_problem"], FAKE_FLAGS_REASON
        return [], FAKE_NO_FLAGS_REASON


def _fake_situation(dispatch_json) -> str:
    """The first procedure in the plan_dispatch result, or FAKE_FALLBACK_SITUATION when it has none."""
    try:
        procedures = json.loads(dispatch_json).get("procedures")
    except (TypeError, ValueError, AttributeError):
        procedures = None
    if isinstance(procedures, list) and procedures and isinstance(procedures[0], str):
        return procedures[0]
    return FAKE_FALLBACK_SITUATION


def _to_anthropic(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant" and m.get("replay") is not None:
            out.append({"role": "assistant", "content": m["replay"]})
        elif m["role"] == "assistant":
            blocks: list[dict] = []
            if m.get("text"):
                blocks.append({"type": "text", "text": m["text"]})
            for c in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": c.id, "name": c.name, "input": c.args})
            out.append({"role": "assistant", "content": blocks})
        else:
            result = {"type": "tool_result", "tool_use_id": m["call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(result)
            else:
                out.append({"role": "user", "content": [result]})
    return out


def _to_openai(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("text") or None}
            calls = [{"id": c.id, "type": "function",
                      "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                     for c in m.get("tool_calls", [])]
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:
            out.append({"role": "tool", "tool_call_id": m["call_id"], "content": m["content"]})
    return out


async def _post(name: str, url: str, headers: dict, body: dict, transport) -> dict:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=PROVIDER_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError:
        raise ProviderError(f"could not reach {name}") from None
    if resp.status_code != 200:
        raise _HTTPStatus(name, resp.status_code, _mentions_strict(resp), _mentions_tool_choice(resp))
    try:
        return resp.json()
    except ValueError:
        raise ProviderError(f"unexpected response from {name}") from None


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, transport=None):
        self._key, self._model, self._transport = api_key, model, transport

    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        body = {
            "model": self._model,
            "max_tokens": AGENT_MAX_TOKENS,
            "output_config": {"effort": AGENT_EFFORT},
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                      for t in tools],
            "messages": _to_anthropic(messages),
        }
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        data = await _post(self.name, ANTHROPIC_URL, headers, body, self._transport)
        try:
            stop_reason = data.get("stop_reason")
            if stop_reason not in ANTHROPIC_TURN_STOPS:
                raise ValueError("stop_reason")
            content = data["content"]
            if not isinstance(content, list):
                raise TypeError("content")
            text = "".join(b["text"] for b in content if b["type"] == "text")
            calls = [ToolCall(b["id"], b["name"], b["input"]) for b in content if b["type"] == "tool_use"]
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError(f"unexpected response from {self.name}") from None
        return Turn(text, calls, copy.deepcopy(content))

    async def _forced_tool(self, system: str, prompt: str, tool: dict, keys: tuple[str, str]) -> tuple:
        body = {"model": self._model, "max_tokens": CHOICE_MAX_TOKENS, "output_config": {"effort": CHOICE_EFFORT},
                "system": [{"type": "text", "text": f"{system} {TOOL_ONLY_INSTRUCTION}",
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": prompt}]}
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        enabled = _strict_enabled()
        with _STRICT_LOCK:
            state = _STRICT_STATE.get(self._model)
            probe = enabled and state is None and self._model not in _STRICT_PROBE
            if probe:
                _STRICT_PROBE.add(self._model)
            choice_state = _CHOICE_STATE.get(self._model)
            choice_probe = choice_state is None and self._model not in _CHOICE_PROBE
            if choice_probe:
                _CHOICE_PROBE.add(self._model)
        strict = enabled and (state == "ok" or probe)
        forced = choice_state == "forced" or choice_probe
        dropped_strict = dropped_forced = False
        try:
            try:
                data = await _post(self.name, ANTHROPIC_URL, headers, _choice_body(body, tool, strict, forced),
                                   self._transport)
            except _HTTPStatus as exc:
                dropped_strict = strict and exc.status == 400 and exc.mentions_strict
                dropped_forced = forced and exc.status == 400 and exc.mentions_tool_choice
                if not (dropped_strict or dropped_forced):
                    raise
                strict, forced = strict and not dropped_strict, forced and not dropped_forced
                data = await _post(self.name, ANTHROPIC_URL, headers, _choice_body(body, tool, strict, forced),
                                   self._transport)
            if dropped_strict:
                _set_strict(self._model, "refused")
            elif strict and probe:
                _set_strict(self._model, "ok")
            if dropped_forced:
                _set_choice(self._model, "auto")
            elif forced and choice_probe:
                _set_choice(self._model, "forced")
        finally:
            with _STRICT_LOCK:
                if probe:
                    _STRICT_PROBE.discard(self._model)
                if choice_probe:
                    _CHOICE_PROBE.discard(self._model)
        try:
            stop_reason = data.get("stop_reason")
            if stop_reason != "tool_use":
                raise ProviderError(f"unexpected response from {self.name}: stop_reason {stop_reason!r}")
            # Other blocks (text, or thinking a model may send before the tool call) never feed the answer.
            blocks = [b for b in data["content"] if b.get("type") == "tool_use"]
            if len(blocks) != 1 or blocks[0].get("name") != tool["name"]:
                raise ValueError("blocks")
            answer = blocks[0]["input"]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError(f"unexpected response from {self.name}") from None
        return _answer_fields(self.name, answer, keys)

    async def choose(self, request: ChoiceRequest) -> tuple:
        tool = {"name": CHOICE_TOOL, "description": CHOICE_TOOL_DESCRIPTION,
                "input_schema": choice_schema([o.id for o in request.options])}
        return await self._forced_tool(CHOICE_SYSTEM, choice_prompt(request), tool, ("choice", "reason"))

    async def choose_many(self, request: ChoiceRequest) -> tuple:
        tool = {"name": MANY_TOOL, "description": MANY_TOOL_DESCRIPTION,
                "input_schema": many_schema([o.id for o in request.options])}
        return await self._forced_tool(FLAGS_SYSTEM, many_prompt(request), tool, ("choices", "reason"))


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, transport=None):
        self._key, self._model, self._transport = api_key, model, transport

    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> Turn:
        body = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}] + _to_openai(messages),
            "tools": [{"type": "function",
                       "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
                      for t in tools],
        }
        headers = {"Authorization": f"Bearer {self._key}", "content-type": "application/json"}
        data = await _post(self.name, OPENAI_URL, headers, body, self._transport)
        try:
            msg = data["choices"][0]["message"]
            calls = [ToolCall(c["id"], c["function"]["name"], _json_args(c["function"]["arguments"]))
                     for c in (msg.get("tool_calls") or [])]
            text = msg.get("content") or ""
            if not isinstance(text, str):
                raise TypeError("content")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError(f"unexpected response from {self.name}") from None
        return Turn(text, calls)

    async def _structured(self, system: str, prompt: str, schema_name: str, schema: dict,
                          keys: tuple[str, str]) -> tuple:
        body = {"model": self._model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
                "max_completion_tokens": OPENAI_CHOICE_MAX_TOKENS}
        headers = {"Authorization": f"Bearer {self._key}", "content-type": "application/json"}
        data = await _post(self.name, OPENAI_URL, headers, body, self._transport)
        try:
            if len(data["choices"]) != 1:
                raise ValueError("choices")
            first = data["choices"][0]
            finish_reason = first.get("finish_reason")
            if finish_reason != "stop":
                raise ProviderError(f"unexpected response from {self.name}: finish_reason {finish_reason!r}")
            msg = first["message"]
            if msg.get("refusal"):
                raise ProviderError(f"{self.name} refused the choice")
            answer = json.loads(msg["content"])
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProviderError(f"unexpected response from {self.name}") from None
        return _answer_fields(self.name, answer, keys)

    async def choose(self, request: ChoiceRequest) -> tuple:
        return await self._structured(CHOICE_SYSTEM, choice_prompt(request), "choice",
                                      choice_schema([o.id for o in request.options]), ("choice", "reason"))

    async def choose_many(self, request: ChoiceRequest) -> tuple:
        return await self._structured(FLAGS_SYSTEM, many_prompt(request), "choices",
                                      many_schema([o.id for o in request.options]), ("choices", "reason"))


def _json_args(raw) -> dict:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_provider(env: Mapping[str, str] | None = None):
    """The provider named by CARMIND_PROVIDER (default fake); raises ProviderError when it is not set up."""
    env = os.environ if env is None else env
    name = (env.get("CARMIND_PROVIDER") or "fake").strip().lower()
    if name == "fake":
        return FakeProvider()
    if name in ("anthropic", "openai"):
        prefix = name.upper()
        key = env.get(f"{prefix}_API_KEY") or ""
        model = env.get(f"CARMIND_{prefix}_MODEL") or ""
        if not key:
            raise ProviderError(f"CARMIND_PROVIDER is {name} but {prefix}_API_KEY is not set. Put it in .env.")
        if not model:
            raise ProviderError(f"CARMIND_{prefix}_MODEL is not set. Put a model id in .env.")
        return (AnthropicProvider if name == "anthropic" else OpenAIProvider)(key, model)
    raise ProviderError(f"Unknown CARMIND_PROVIDER {name!r}: use fake, anthropic or openai.")
