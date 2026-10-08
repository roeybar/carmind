"""
Providers against httpx.MockTransport only: request shapes, response parsing, errors, selection.
"""
import asyncio
import copy
import json
import re
from pathlib import Path

import httpx
import pytest

from app import luna, providers
from app.match import _TABLE, _kw_matches
from app.providers import (
    ASK,
    CHOICE_SYSTEM,
    CHOICE_TOOL_DESCRIPTION,
    FAKE_ASK_REASON,
    FAKE_FLAGS_REASON,
    FAKE_NO_FLAGS_REASON,
    FAKE_PICK_REASON,
    FLAGS_SYSTEM,
    MANY_TOOL_DESCRIPTION,
    AnthropicProvider,
    ChoiceRequest,
    FakeProvider,
    OpenAIProvider,
    Option,
    ProviderError,
    ToolCall,
    choice_prompt,
    choice_schema,
    get_provider,
    many_prompt,
    many_schema,
)

TOOLS = [{"name": "get_customer", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
APP_DIR = Path(providers.__file__).resolve().parent
LP_EN = "coolant warning came on after the engine control unit was updated"
LP_HE = "נורת נוזל קירור נדלקה אחרי עדכון של מחשב המנוע"
LD_EN = "check engine light came on after I topped up the coolant"
LF_EN = "check engine light came on again"
LF_HE = "נורת מנוע דלקה שוב"
LIGHT_IDS = ("coolant_level_low", "engine_control_petrol")
FLAG_IDS = ("repeat_problem", "recent_long_service", "near_service_interval", "fresh_handover", "no_pre_delivery_test")
MODEL = "test-model"


@pytest.fixture(autouse=True)
def _reset_strict_cache(monkeypatch):
    monkeypatch.setattr(providers, "_STRICT_STATE", {})
    monkeypatch.setattr(providers, "_STRICT_PROBE", set())
    monkeypatch.setattr(providers, "_CHOICE_STATE", {}, raising=False)
    monkeypatch.setattr(providers, "_CHOICE_PROBE", set(), raising=False)
    monkeypatch.delenv("CARMIND_ANTHROPIC_STRICT", raising=False)


def _complete(provider, messages=None):
    return asyncio.run(provider.complete("sys", messages or [{"role": "user", "content": "hi"}], TOOLS))


def _mock(status, payload, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def test_anthropic_request_shape_and_tool_use_response():
    seen: list[httpx.Request] = []
    payload = {"content": [{"type": "text", "text": "ok"},
                           {"type": "tool_use", "id": "t1", "name": "get_customer", "input": {"customer_id": "C-1001"}}],
               "stop_reason": "tool_use"}
    turn = _complete(AnthropicProvider("test-key", "test-model", transport=_mock(200, payload, seen)))
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "test-key"
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "test-model"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"] == [{"name": "get_customer", "description": "d", "input_schema": TOOLS[0]["input_schema"]}]
    assert turn.text == "ok"
    assert turn.tool_calls == [ToolCall("t1", "get_customer", {"customer_id": "C-1001"})]


def test_anthropic_text_response():
    turn = _complete(AnthropicProvider("k", "m", transport=_mock(200, {"content": [{"type": "text", "text": "done"}],
                                                                        "stop_reason": "end_turn"})))
    assert turn.text == "done"
    assert turn.tool_calls == []


def test_to_anthropic_groups_consecutive_tool_results():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("a", "get_customer", {}), ToolCall("b", "plan_dispatch", {})]},
        {"role": "tool", "call_id": "a", "name": "get_customer", "content": "{}"},
        {"role": "tool", "call_id": "b", "name": "plan_dispatch", "content": "{}"},
    ]
    out = providers._to_anthropic(messages)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["type"] for b in out[1]["content"]] == ["tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]


def test_openai_parses_string_arguments_and_bad_json():
    seen: list[httpx.Request] = []
    payload = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "1", "type": "function", "function": {"name": "get_customer", "arguments": "{\"customer_id\": \"C-1001\"}"}},
        {"id": "2", "type": "function", "function": {"name": "plan_dispatch", "arguments": "not json"}}]}}]}
    turn = _complete(OpenAIProvider("test-key", "test-model", transport=_mock(200, payload, seen)))
    body = json.loads(seen[0].content)
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert "max_tokens" not in body
    assert body["tools"][0]["function"]["name"] == "get_customer"
    assert turn.tool_calls == [ToolCall("1", "get_customer", {"customer_id": "C-1001"}), ToolCall("2", "plan_dispatch", {})]


def test_openai_assistant_and_tool_messages():
    out = providers._to_openai([
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("1", "get_customer", {"a": 1})]},
        {"role": "assistant", "text": "plain", "tool_calls": []},
        {"role": "tool", "call_id": "1", "name": "get_customer", "content": "{}"},
    ])
    assert out[0]["content"] is None
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{\"a\": 1}"
    assert "tool_calls" not in out[1]
    assert out[2] == {"role": "tool", "tool_call_id": "1", "content": "{}"}


@pytest.mark.parametrize("cls", [AnthropicProvider, OpenAIProvider])
def test_http_error_names_the_status_but_not_the_key(cls):
    with pytest.raises(ProviderError) as exc:
        _complete(cls("secret-key-123", "m", transport=_mock(401, {"error": "bad"})))
    assert "HTTP 401" in str(exc.value)
    assert "secret-key-123" not in str(exc.value)


@pytest.mark.parametrize("cls", [AnthropicProvider, OpenAIProvider])
def test_unreachable_and_malformed_responses_are_provider_errors(cls):
    def boom(request):
        raise httpx.ConnectError("down")
    with pytest.raises(ProviderError, match="could not reach"):
        _complete(cls("k", "m", transport=httpx.MockTransport(boom)))
    with pytest.raises(ProviderError, match="unexpected response"):
        _complete(cls("k", "m", transport=_mock(200, {"nothing": 1, "stop_reason": "end_turn"})))


def test_get_provider_selection():
    assert isinstance(get_provider({}), FakeProvider)
    assert isinstance(get_provider({"CARMIND_PROVIDER": ""}), FakeProvider)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        get_provider({"CARMIND_PROVIDER": "anthropic"})
    with pytest.raises(ProviderError, match="CARMIND_ANTHROPIC_MODEL"):
        get_provider({"CARMIND_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"})
    with pytest.raises(ProviderError, match="CARMIND_OPENAI_MODEL"):
        get_provider({"CARMIND_PROVIDER": "openai", "OPENAI_API_KEY": "k"})
    with pytest.raises(ProviderError, match="OPENAI_API_KEY"):
        get_provider({"CARMIND_PROVIDER": "openai", "CARMIND_OPENAI_MODEL": "m"})
    with pytest.raises(ProviderError, match="Unknown CARMIND_PROVIDER"):
        get_provider({"CARMIND_PROVIDER": "nope"})
    assert isinstance(get_provider({"CARMIND_PROVIDER": "Anthropic", "ANTHROPIC_API_KEY": "k",
                                    "CARMIND_ANTHROPIC_MODEL": "m"}), AnthropicProvider)
    assert isinstance(get_provider({"CARMIND_PROVIDER": "openai", "OPENAI_API_KEY": "k",
                                    "CARMIND_OPENAI_MODEL": "m"}), OpenAIProvider)


def test_fake_provider_asks_for_the_screen_tools_then_the_procedure_then_writes_the_note():
    fake = FakeProvider()
    first = asyncio.run(fake.complete("s", [{"role": "user", "content": "q"}], TOOLS))
    assert [c.name for c in first.tool_calls] == ["get_customer", "assess_warning_light", "plan_dispatch"]
    results = {
        "get_customer": {"name": "John Doe"},
        "assess_warning_light": {"vehicle": "2023 Skoda Octavia (petrol)"},
        "plan_dispatch": {"action": "tow_to_dealer", "covered": True, "procedures": ["not_drivable", "after_hours"]},
    }
    messages = [{"role": "tool", "call_id": n, "name": n, "content": json.dumps(r)} for n, r in results.items()]
    second = asyncio.run(fake.complete("s", messages, TOOLS))
    assert second.tool_calls == [ToolCall("fake-4", "get_company_procedure", {"situation": "not_drivable"})]
    assert second.text == ""
    messages.append({"role": "tool", "call_id": "fake-4", "name": "get_company_procedure",
                     "content": json.dumps({"situation": "not_drivable", "steps": ["a"]})})
    third = asyncio.run(fake.complete("s", messages, TOOLS))
    assert third.tool_calls == []
    assert third.text == providers.FAKE_NOTE == "I looked up the customer, the car and the company procedure."


@pytest.mark.parametrize("dispatch_content", ['{"error": "tool_failed"}', "not json", "[1]", '{"procedures": []}', None])
def test_fake_provider_falls_back_to_unclear_description(dispatch_content):
    messages = [{"role": "tool", "call_id": "get_customer", "name": "get_customer",
                 "content": json.dumps({"name": "John Doe"})}]
    if dispatch_content is not None:
        messages.append({"role": "tool", "call_id": "plan_dispatch", "name": "plan_dispatch",
                         "content": dispatch_content})
    turn = asyncio.run(FakeProvider().complete("s", messages, TOOLS))
    assert [(c.name, c.args) for c in turn.tool_calls] == [("get_company_procedure", {"situation": "unclear_description"})]


def test_no_model_id_literal_in_the_code():
    for name in ("providers.py", "agent.py", "luna.py", "orchestrator.py", "desk.py"):
        source = (APP_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"claude-[a-z]+-\d", source)
        assert not re.search(r"gpt-\d", source)


def _options(text, ids=LIGHT_IDS):
    lower = text.lower()
    return tuple(Option(i, i, f"{i}: meaning", tuple(kw for row in _TABLE if row[0] == i
                                                    for kw in (*row[1], *row[2]) if _kw_matches(kw, lower)))
                 for i in ids)


def _request(text=LP_EN):
    return ChoiceRequest("Which light?", text, _options(text))


def _many_request(text=LF_EN):
    return ChoiceRequest("Which facts?", text, tuple(Option(f, f, f"{f} text") for f in FLAG_IDS))


def _choose(provider, request=None):
    return asyncio.run(provider.choose(request or _request()))


def _choose_many(provider, request=None):
    return asyncio.run(provider.choose_many(request or _many_request()))


def _tool_payload(name, answer, **extra):
    return {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": answer}],
            "stop_reason": "tool_use", **extra}


def _openai_payload(answer, finish_reason="stop"):
    content = answer if isinstance(answer, str) else json.dumps(answer)
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}


def _error(message):
    return {"error": {"type": "invalid_request_error", "message": message}}


def _thinking(sig):
    return {"type": "thinking", "thinking": "plan the lookups", "signature": sig}


REDACTED = {"type": "redacted_thinking", "data": "opaque"}
FUTURE = {"type": "future_block", "data": "kept"}
SAMPLING = ("thinking", "temperature", "top_p", "top_k", "stop_sequences")
TC_400 = (400, _error('tool_choice: type "tool" and "any" are not supported for this model.'))


def _sequence(*responses, seen=None):
    queue = list(responses)

    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def test_choice_schema_lists_the_options_then_ask():
    schema = choice_schema(list(LIGHT_IDS))
    assert list(schema["properties"]) == ["reason", "choice"]
    assert schema["required"] == ["reason", "choice"]
    assert schema["properties"]["choice"]["enum"] == [*LIGHT_IDS, ASK] == [*LIGHT_IDS, "ask"]
    assert schema["additionalProperties"] is False


def test_many_schema_and_prompt():
    schema = many_schema(list(FLAG_IDS))
    assert list(schema["properties"]) == ["reason", "choices"]
    assert schema["required"] == ["reason", "choices"]
    assert schema["properties"]["choices"]["type"] == "array"
    assert schema["properties"]["choices"]["items"]["enum"] == list(FLAG_IDS)
    assert schema["additionalProperties"] is False
    lines = many_prompt(_many_request()).split("\n")
    assert lines[:5] == ["Which facts?", "", f"The caller's words, masked: {LF_EN}", "",
                         "Facts you may choose (any number, or none):"]
    assert lines[5:] == [f"- {f}: {f} text" for f in FLAG_IDS]


def test_choice_prompt_lists_each_option_and_ask():
    request = ChoiceRequest("Which light?", "ctx", (Option("a_id", "A", "A: meaning.", ("w1", "w2")),
                                                    Option("b_id", "B", "B: meaning.")))
    assert choice_prompt(request).split("\n") == [
        "Which light?", "", "The caller's words, masked: ctx", "", "Options:",
        "- a_id: A: meaning. Matched words: w1, w2.",
        "- b_id: B: meaning. Matched words: none.",
        "- ask: the words do not point to exactly one of the options above, so carmind asks the caller.",
    ]


def test_fake_choose_rule():
    fake = FakeProvider()
    for text, expected in ((LP_EN, "coolant_level_low"), (LP_HE, "coolant_level_low"),
                           (LD_EN, "engine_control_petrol"), (LP_EN.upper(), "coolant_level_low")):
        assert _choose(fake, _request(text)) == (expected, FAKE_PICK_REASON), text
    for text in ("coolant light and check engine light both on", "the coolant and the check engine were checked"):
        assert _choose(fake, _request(text)) == (ASK, FAKE_ASK_REASON), text


def test_fake_choose_many_rule():
    fake = FakeProvider()
    for text in (LF_EN, LF_HE):
        assert _choose_many(fake, _many_request(text)) == (["repeat_problem"], FAKE_FLAGS_REASON), text
    for text in ("חשוב מאוד: נורת מנוע", LP_EN, "the light was against the glare"):
        assert _choose_many(fake, _many_request(text)) == ([], FAKE_NO_FLAGS_REASON), text


def test_anthropic_choose_request_shape_and_answer():
    seen: list[httpx.Request] = []
    payload = _tool_payload("choose", {"reason": "r", "choice": "coolant_level_low"})
    request = _request()
    answer = _choose(AnthropicProvider("test-key", MODEL, transport=_mock(200, payload, seen)), request)
    assert answer == ("coolant_level_low", "r")
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert "anthropic-beta" not in seen[0].headers
    assert body["tools"] == [{"name": "choose", "description": CHOICE_TOOL_DESCRIPTION,
                              "input_schema": choice_schema(list(LIGHT_IDS)), "strict": True}]
    assert body["tool_choice"] == {"type": "tool", "name": "choose"}
    assert body["system"] == [{"type": "text", "text": f"{CHOICE_SYSTEM} {providers.TOOL_ONLY_INSTRUCTION}",
                               "cache_control": {"type": "ephemeral"}}]
    assert body["max_tokens"] == providers.CHOICE_MAX_TOKENS == 4096
    assert body["messages"] == [{"role": "user", "content": choice_prompt(request)}]
    assert providers._STRICT_STATE[MODEL] == "ok"
    assert providers._CHOICE_STATE[MODEL] == "forced"


def test_anthropic_choose_many_shape():
    seen: list[httpx.Request] = []
    payload = _tool_payload("choose_many", {"reason": "r", "choices": ["repeat_problem"]})
    request = _many_request()
    answer = _choose_many(AnthropicProvider("k", MODEL, transport=_mock(200, payload, seen)), request)
    assert answer == (["repeat_problem"], "r")
    body = json.loads(seen[0].content)
    assert body["tools"] == [{"name": "choose_many", "description": MANY_TOOL_DESCRIPTION,
                              "input_schema": many_schema(list(FLAG_IDS)), "strict": True}]
    assert body["tool_choice"] == {"type": "tool", "name": "choose_many"}
    assert body["system"][0]["text"] == f"{FLAGS_SYSTEM} {providers.TOOL_ONLY_INSTRUCTION}"
    assert body["messages"] == [{"role": "user", "content": many_prompt(request)}]


def test_anthropic_verified_strict_refusal_is_recorded_after_a_plain_success():
    seen: list[dict] = []
    ok = (200, _tool_payload("choose", {"reason": "r", "choice": "coolant_level_low"}))
    refusal = (400, _error("strict mode is not supported for this model"))
    provider = AnthropicProvider("k", MODEL, transport=_sequence(refusal, ok, seen=seen))
    assert _choose(provider) == ("coolant_level_low", "r")
    assert len(seen) == 2
    assert seen[0]["tools"][0]["strict"] is True
    assert "strict" not in seen[1]["tools"][0]
    assert providers._STRICT_STATE[MODEL] == "refused"
    assert seen[1]["tool_choice"] == {"type": "tool", "name": "choose"}
    assert providers._CHOICE_STATE[MODEL] == "forced"
    assert _choose(provider) == ("coolant_level_low", "r")
    assert len(seen) == 3 and "strict" not in seen[2]["tools"][0]


def test_anthropic_unrelated_400_raises_and_records_nothing():
    seen: list[dict] = []
    provider = AnthropicProvider("k", MODEL, transport=_sequence((400, _error("messages: bad")), seen=seen))
    with pytest.raises(ProviderError):
        _choose(provider)
    assert len(seen) == 1
    assert providers._STRICT_STATE == {}


def test_anthropic_failed_plain_retry_records_nothing():
    seen: list[dict] = []
    refusal = (400, _error("strict mode is not supported for this model"))
    provider = AnthropicProvider("k", MODEL, transport=_sequence(refusal, (500, {"error": "x"}), seen=seen))
    with pytest.raises(ProviderError):
        _choose(provider)
    assert len(seen) == 2
    assert providers._STRICT_STATE == {}


def test_anthropic_server_error_does_not_retry():
    seen: list[dict] = []
    provider = AnthropicProvider("k", MODEL, transport=_sequence((500, {"error": "x"}), seen=seen))
    with pytest.raises(ProviderError):
        _choose(provider)
    assert len(seen) == 1
    assert providers._STRICT_STATE == {}


def test_anthropic_strict_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("CARMIND_ANTHROPIC_STRICT", "off")
    seen: list[dict] = []
    ok = (200, _tool_payload("choose", {"reason": "r", "choice": "coolant_level_low"}))
    provider = AnthropicProvider("k", MODEL, transport=_sequence(ok, seen=seen))
    assert _choose(provider) == ("coolant_level_low", "r")
    assert len(seen) == 1 and "strict" not in seen[0]["tools"][0]
    assert providers._STRICT_STATE == {}


def test_anthropic_concurrent_first_use_probes_once():
    seen: list[bool] = []
    in_flight = {"now": 0, "max": 0}

    async def main():
        strict_seen, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            strict = json.loads(request.content)["tools"][0].get("strict") is True
            seen.append(strict)
            if strict:
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])
                strict_seen.set()
                await release.wait()
                in_flight["now"] -= 1
            return httpx.Response(200, json=_tool_payload("choose", {"reason": "r", "choice": "coolant_level_low"}))

        provider = AnthropicProvider("k", MODEL, transport=httpx.MockTransport(handler))
        first = asyncio.create_task(provider.choose(_request()))
        await strict_seen.wait()
        second = await asyncio.wait_for(provider.choose(_request()), 5)
        release.set()
        assert await first == ("coolant_level_low", "r")
        assert second == ("coolant_level_low", "r")
        assert providers._STRICT_STATE[MODEL] == "ok"
        await provider.choose(_request())

    asyncio.run(main())
    assert seen == [True, False, True]
    assert in_flight["max"] == 1


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_malformed_envelopes_are_provider_errors(method):
    if method == "choose":
        name, good, call = "choose", {"reason": "r", "choice": "coolant_level_low"}, _choose
    else:
        name, good, call = "choose_many", {"reason": "r", "choices": ["repeat_problem"]}, _choose_many
    key = next(k for k in good if k != "reason")
    block = {"type": "tool_use", "id": "t1", "name": name, "input": good}
    for payload in (
        _tool_payload(name, ["coolant_level_low"]),
        _tool_payload(name, {"reason": "r"}),
        _tool_payload(name, {key: good[key]}),
        _tool_payload(name, {**good, "extra": 1}),
        {"content": [block, block], "stop_reason": "tool_use"},
        _tool_payload(name, good, stop_reason="max_tokens"),
        {"content": [{"type": "text", "text": "coolant_level_low"}], "stop_reason": "end_turn"},
    ):
        with pytest.raises(ProviderError, match="unexpected response"):
            call(AnthropicProvider("k", MODEL, transport=_mock(200, payload)))


def _good(method):
    if method == "choose":
        return {"reason": "r", "choice": "coolant_level_low"}, _choose
    return {"reason": "r", "choices": ["repeat_problem"]}, _choose_many


def _anthropic_unsuccessful(name, good):
    block = {"type": "tool_use", "id": "t1", "name": name, "input": good}
    return {
        "refusal": {"content": [block], "stop_reason": "refusal"},
        "no_stop_reason": {"content": [block]},
        "extra_tool_use": {"content": [block, {"type": "tool_use", "id": "t2", "name": "other", "input": {}}],
                           "stop_reason": "tool_use"},
    }


def _openai_unsuccessful(good):
    choice = {"finish_reason": "stop", "message": {"content": json.dumps(good)}}
    return {
        "content_filter": {"choices": [{**choice, "finish_reason": "content_filter"}]},
        "no_finish_reason": {"choices": [{"message": choice["message"]}]},
        "two_choices": {"choices": [choice, choice]},
    }


@pytest.mark.parametrize("case", ["refusal", "no_stop_reason", "extra_tool_use"])
@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_unsuccessful_envelopes_are_provider_errors(method, case):
    good, call = _good(method)
    payload = _anthropic_unsuccessful(method, good)[case]
    with pytest.raises(ProviderError):
        call(AnthropicProvider("k", MODEL, transport=_mock(200, payload)))


@pytest.mark.parametrize("case", ["content_filter", "no_finish_reason", "two_choices"])
@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_openai_unsuccessful_envelopes_are_provider_errors(method, case):
    good, call = _good(method)
    payload = _openai_unsuccessful(good)[case]
    with pytest.raises(ProviderError):
        call(OpenAIProvider("k", MODEL, transport=_mock(200, payload)))


def test_openai_choose_request_shape_and_answer():
    seen: list[httpx.Request] = []
    request = _request()
    payload = _openai_payload({"reason": "r", "choice": "coolant_level_low"})
    answer = _choose(OpenAIProvider("test-key", MODEL, transport=_mock(200, payload, seen)), request)
    assert answer == ("coolant_level_low", "r")
    body = json.loads(seen[0].content)
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "choice", "strict": True, "schema": choice_schema(list(LIGHT_IDS))}}
    assert body["max_completion_tokens"] == 1024
    assert "tools" not in body
    assert body["messages"] == [{"role": "system", "content": CHOICE_SYSTEM},
                                {"role": "user", "content": choice_prompt(request)}]


def test_openai_choose_many_shape():
    seen: list[httpx.Request] = []
    request = _many_request()
    payload = _openai_payload({"reason": "r", "choices": ["repeat_problem"]})
    answer = _choose_many(OpenAIProvider("k", MODEL, transport=_mock(200, payload, seen)), request)
    assert answer == (["repeat_problem"], "r")
    body = json.loads(seen[0].content)
    assert body["response_format"]["json_schema"] == {"name": "choices", "strict": True,
                                                      "schema": many_schema(list(FLAG_IDS))}
    assert body["messages"] == [{"role": "system", "content": FLAGS_SYSTEM},
                                {"role": "user", "content": many_prompt(request)}]


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_openai_malformed_envelopes_are_provider_errors(method):
    if method == "choose":
        good, call = {"reason": "r", "choice": "coolant_level_low"}, _choose
    else:
        good, call = {"reason": "r", "choices": ["repeat_problem"]}, _choose_many
    for payload in (
        _openai_payload(good, finish_reason="length"),
        _openai_payload({**good, "extra": 1}),
        _openai_payload({"reason": "r"}),
        {"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "I cannot help."}}]},
        _openai_payload("not json"),
        _openai_payload("[1]"),
    ):
        with pytest.raises(ProviderError):
            call(OpenAIProvider("k", MODEL, transport=_mock(200, payload)))


@pytest.mark.parametrize("cls", [AnthropicProvider, OpenAIProvider])
def test_answer_key_order_is_not_relied_on(cls):
    answer = {"choice": "coolant_level_low", "reason": "r"}
    payload = _tool_payload("choose", answer) if cls is AnthropicProvider else _openai_payload(answer)
    assert _choose(cls("k", MODEL, transport=_mock(200, payload))) == ("coolant_level_low", "r")
    answer = {"choices": ["repeat_problem"], "reason": "r"}
    payload = _tool_payload("choose_many", answer) if cls is AnthropicProvider else _openai_payload(answer)
    assert _choose_many(cls("k", MODEL, transport=_mock(200, payload))) == (["repeat_problem"], "r")


@pytest.mark.parametrize("cls", [AnthropicProvider, OpenAIProvider])
def test_choose_http_errors_name_the_status_not_the_key(cls):
    for call in (_choose, _choose_many):
        with pytest.raises(ProviderError) as exc:
            call(cls("secret-key-123", MODEL, transport=_mock(401, {"error": "bad"})))
        assert "HTTP 401" in str(exc.value)
        assert "secret-key-123" not in str(exc.value)


# Haiku 5.5 and Sonnet 5.5: thinking replay, effort, caps and stop reasons
def _tool_use(name, answer, call_id="t1"):
    return {"type": "tool_use", "id": call_id, "name": name, "input": answer}


def test_anthropic_complete_sends_effort_and_the_cap_and_no_sampling_or_thinking_fields():
    seen: list[httpx.Request] = []
    payload = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "text": "", "tool_calls": [ToolCall("t1", "get_customer", {})],
         "replay": [_thinking("sig-one"), _tool_use("get_customer", {})]},
        {"role": "tool", "call_id": "t1", "name": "get_customer", "content": "{}"},
    ]
    _complete(AnthropicProvider("k", MODEL, transport=_mock(200, payload, seen)), messages)
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == providers.AGENT_MAX_TOKENS == 8192
    assert body["output_config"] == {"effort": "medium"}
    assert not [k for k in SAMPLING if k in body]
    assert body["messages"][-1]["role"] == "user"
    assert "anthropic-beta" not in seen[0].headers


def test_anthropic_complete_replays_thinking_blocks_unchanged_across_a_tool_turn():
    seen: list[dict] = []
    payload1 = {"content": [_thinking("sig-one"), REDACTED, FUTURE, {"type": "text", "text": "Looking."},
                            _tool_use("get_customer", {"customer_id": "C-1001"})], "stop_reason": "tool_use"}
    payload2 = {"content": [_thinking("sig-two"), {"type": "text", "text": "Done."}], "stop_reason": "end_turn"}
    expected = copy.deepcopy(payload1["content"])
    provider = AnthropicProvider("k", MODEL, transport=_sequence((200, payload1), (200, payload2), seen=seen))
    turn = _complete(provider)
    assert turn.replay == expected
    turn.tool_calls[0].args["customer_id"] = "changed"
    assert turn.replay == expected
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant", "text": turn.text, "tool_calls": turn.tool_calls, "replay": turn.replay},
                {"role": "tool", "call_id": "t1", "name": "get_customer", "content": "{}"}]
    second = _complete(provider, messages)
    assert second.text == "Done."
    body1, body2 = seen
    assert body2["messages"] == [{"role": "user", "content": "hi"}, {"role": "assistant", "content": expected},
                                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                               "content": "{}"}]}]
    assert body1["messages"] == body2["messages"][:1]


@pytest.mark.parametrize("stop_reason,content", [
    ("refusal", [{"type": "text", "text": "I can't help with that."}]),
    ("max_tokens", [_thinking("s")]),
    ("max_tokens", [_thinking("s"), {"type": "text", "text": "partial"}]),
    ("model_context_window_exceeded", [{"type": "text", "text": "x"}]),
    (None, [{"type": "text", "text": "ok"}]),
])
def test_anthropic_complete_refusal_and_cap_are_provider_errors(stop_reason, content):
    payload = {"content": content}
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    with pytest.raises(ProviderError) as exc:
        _complete(AnthropicProvider("k", MODEL, transport=_mock(200, payload)))
    assert str(exc.value) == "unexpected response from anthropic"


@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use"])
def test_anthropic_complete_accepts_end_turn_and_tool_use(stop_reason):
    payload = {"content": [_thinking("s"), {"type": "text", "text": "ok"}], "stop_reason": stop_reason}
    turn = _complete(AnthropicProvider("k", MODEL, transport=_mock(200, payload)))
    assert turn.text == "ok"


def _call_method(method):
    good, call = _good(method)
    return good, call, (200, _tool_payload(method, good))


@pytest.mark.parametrize("strict", ["on", "off"])
@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_choice_calls_send_effort_and_the_budgeted_cap(monkeypatch, method, strict):
    monkeypatch.setenv("CARMIND_ANTHROPIC_STRICT", strict)
    good, call, ok = _call_method(method)
    seen: list[dict] = []
    assert call(AnthropicProvider("k", MODEL, transport=_sequence(ok, seen=seen))) == _answer(method, good)
    body = seen[0]
    assert body["max_tokens"] == providers.CHOICE_MAX_TOKENS == 4096
    assert body["output_config"] == {"effort": "medium"}
    assert not [k for k in SAMPLING if k in body]
    assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
    if strict == "on":
        refused: list[dict] = []
        refusal = (400, _error("strict mode is not supported for this model"))
        call(AnthropicProvider("k", MODEL, transport=_sequence(refusal, ok, seen=refused)))
        assert len(refused) == 2
        for sent in refused:
            assert sent["max_tokens"] == providers.CHOICE_MAX_TOKENS
            assert sent["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("strict", ["on", "off"])
@pytest.mark.parametrize("block", [_thinking("s"), REDACTED])
@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_choice_ignores_thinking_before_the_tool_call(monkeypatch, method, block, strict):
    monkeypatch.setenv("CARMIND_ANTHROPIC_STRICT", strict)
    good, call = _good(method)
    payload = {"content": [block, _tool_use(method, good)], "stop_reason": "tool_use"}
    assert call(AnthropicProvider("k", MODEL, transport=_mock(200, payload))) == _answer(method, good)


def _answer(method, good):
    return (good["choice"], good["reason"]) if method == "choose" else (good["choices"], good["reason"])


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_choice_envelope_is_not_loosened_by_thinking(method):
    good, call = _good(method)
    block = _tool_use(method, good)
    for content, stop_reason in (
        ([_thinking("s")], "max_tokens"),
        ([_thinking("s"), block, block], "tool_use"),
        ([_thinking("s"), block], "refusal"),
        ([_thinking("s"), {"type": "text", "text": "coolant_level_low"}], "end_turn"),
    ):
        with pytest.raises(ProviderError):
            call(AnthropicProvider("k", MODEL, transport=_mock(200, {"content": content, "stop_reason": stop_reason})))


def test_env_example_names_haiku_5_5():
    path = next(p for p in (APP_DIR.parent / "env.example", APP_DIR.parents[1] / ".env.example") if p.exists())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("CARMIND_ANTHROPIC_MODEL=")] == [
        "CARMIND_ANTHROPIC_MODEL=claude-haiku-5-5"]


def test_anthropic_concurrent_conversations_stay_append_only():
    convs = [f"conv-{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(32)]
    bodies: dict[str, list[dict]] = {c: [] for c in convs}
    served: dict[str, list[list]] = {c: [] for c in convs}

    async def handler(request):
        await asyncio.sleep(0)
        body = json.loads(request.content)
        conv = body["messages"][0]["content"]
        k = sum(1 for m in body["messages"] if m["role"] == "assistant")
        content = [_thinking(f"sig-{conv}-{k}"), _tool_use("get_customer", {}, f"t-{conv}-{k}")]
        bodies[conv].append(body)
        served[conv].append(copy.deepcopy(content))
        return httpx.Response(200, json={"content": content, "stop_reason": "tool_use"})

    provider = AnthropicProvider("k", MODEL, transport=httpx.MockTransport(handler))

    async def conversation(conv):
        messages = [{"role": "user", "content": conv}]
        for _ in range(5):
            turn = await provider.complete("sys", messages, TOOLS)
            messages.append({"role": "assistant", "text": turn.text, "tool_calls": list(turn.tool_calls),
                             "replay": turn.replay})
            for c in turn.tool_calls:
                messages.append({"role": "tool", "call_id": c.id, "name": c.name, "content": "{}"})

    async def main():
        await asyncio.gather(*[conversation(c) for c in convs])

    asyncio.run(main())
    for conv in convs:
        sent = bodies[conv]
        assert len(sent) == 5
        for k in range(1, 5):
            before = sent[k - 1]["messages"]
            assert sent[k]["messages"][:len(before)] == before
            assert sent[k]["messages"][len(before)] == {"role": "assistant", "content": served[conv][k - 1]}
        signatures = [b["signature"] for body in sent for m in body["messages"] if m["role"] == "assistant"
                      for b in m["content"] if b["type"] == "thinking"]
        assert len(signatures) == 10
        assert all(s.startswith(f"sig-{conv}-") for s in signatures)


# Forced tool use, and tool_choice auto when a model refuses it
@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_forced_choice_is_accepted_without_a_retry(method):
    good, call, ok = _call_method(method)
    seen: list[dict] = []
    provider = AnthropicProvider("k", MODEL, transport=_sequence(ok, seen=seen))
    assert call(provider) == _answer(method, good)
    assert call(provider) == _answer(method, good)
    assert len(seen) == 2
    for body in seen:
        assert body["tool_choice"] == {"type": "tool", "name": method}
        assert body["tools"][0]["strict"] is True
    assert providers._CHOICE_STATE[MODEL] == "forced"


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_tool_choice_refusal_retries_with_auto_and_is_remembered(method):
    good, call, ok = _call_method(method)
    seen: list[dict] = []
    provider = AnthropicProvider("k", MODEL, transport=_sequence(TC_400, ok, seen=seen))
    assert call(provider) == _answer(method, good)
    assert len(seen) == 2
    assert seen[0]["tool_choice"] == {"type": "tool", "name": method} and seen[0]["tools"][0]["strict"] is True
    assert seen[1]["tool_choice"] == {"type": "auto"} and seen[1]["tools"][0]["strict"] is True
    assert providers._CHOICE_STATE[MODEL] == "auto"
    assert providers._STRICT_STATE[MODEL] == "ok"
    assert call(provider) == _answer(method, good)
    assert len(seen) == 3
    assert seen[2]["tool_choice"] == {"type": "auto"} and seen[2]["tools"][0]["strict"] is True
    for body in seen:
        assert body["max_tokens"] == 4096
        assert body["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_auto_answer_without_the_tool_is_a_failed_outcome(monkeypatch, method):
    providers._CHOICE_STATE[MODEL] = "auto"
    _, call = _good(method)
    payload = {"content": [{"type": "text", "text": "coolant_level_low"}], "stop_reason": "end_turn"}
    provider = AnthropicProvider("k", MODEL, transport=_mock(200, payload))
    with pytest.raises(ProviderError):
        call(provider)
    monkeypatch.setenv("LUNA_MODE", "on")
    if method == "choose":
        outcome = asyncio.run(luna.choose("Which light?", LP_EN, list(_options(LP_EN)), provider))
        assert outcome == luna.Outcome("failed")
    else:
        options = [Option(f, f, f"{f} text") for f in FLAG_IDS]
        outcome = asyncio.run(luna.choose_many("Which facts?", LF_EN, options, provider))
        assert outcome == luna.ManyOutcome("failed")


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_auto_with_thinking_then_the_tool_gives_the_answer(method):
    providers._CHOICE_STATE[MODEL] = "auto"
    good, call = _good(method)
    seen: list[dict] = []
    payload = {"content": [_thinking("s"), {"type": "text", "text": "Calling the tool."}, _tool_use(method, good)],
               "stop_reason": "tool_use"}
    assert call(AnthropicProvider("k", MODEL, transport=_sequence((200, payload), seen=seen))) == _answer(method, good)
    assert seen[0]["tool_choice"] == {"type": "auto"}


def test_anthropic_concurrent_first_choice_calls_probe_forced_once():
    seen: list[str] = []
    in_flight = {"now": 0, "max": 0}

    async def main():
        forced_seen, release = asyncio.Event(), asyncio.Event()

        async def handler(request):
            kind = json.loads(request.content)["tool_choice"]["type"]
            seen.append(kind)
            if kind == "tool":
                in_flight["now"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["now"])
                forced_seen.set()
                await release.wait()
                in_flight["now"] -= 1
            return httpx.Response(200, json=_tool_payload("choose", {"reason": "r", "choice": "coolant_level_low"}))

        provider = AnthropicProvider("k", MODEL, transport=httpx.MockTransport(handler))
        first = asyncio.create_task(provider.choose(_request()))
        await forced_seen.wait()
        second = await asyncio.wait_for(provider.choose(_request()), 5)
        release.set()
        assert await first == ("coolant_level_low", "r")
        assert second == ("coolant_level_low", "r")
        assert providers._CHOICE_STATE[MODEL] == "forced"
        await provider.choose(_request())

    asyncio.run(main())
    assert seen == ["tool", "auto", "tool"]
    assert in_flight["max"] == 1


def _assert_forced_strict_follow_up(method):
    good, call, ok = _call_method(method)
    after: list[dict] = []
    assert call(AnthropicProvider("k", MODEL, transport=_sequence(ok, seen=after))) == _answer(method, good)
    assert len(after) == 1
    assert after[0]["tool_choice"] == {"type": "tool", "name": method}
    assert after[0]["tools"][0]["strict"] is True


@pytest.mark.parametrize("case", ["unrelated", "already_auto", "failed_retry", "second_refusal", "server_error"])
@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_unrelated_or_repeated_400s_record_no_choice_state(method, case):
    _, call = _good(method)
    seen: list[dict] = []
    responses, requests = {
        "unrelated": (((400, _error("messages: bad")),), 1),
        "already_auto": ((TC_400,), 1),
        "failed_retry": ((TC_400, (500, {"error": "x"})), 2),
        "second_refusal": ((TC_400, (400, _error("strict mode is not supported for this model"))), 2),
        "server_error": (((500, TC_400[1]),), 1),
    }[case]
    if case == "already_auto":
        providers._CHOICE_STATE[MODEL] = "auto"
        providers._STRICT_STATE[MODEL] = "ok"
    with pytest.raises(ProviderError):
        call(AnthropicProvider("k", MODEL, transport=_sequence(*responses, seen=seen)))
    assert len(seen) == requests
    if case == "already_auto":
        assert providers._STRICT_STATE == {MODEL: "ok"}
        assert providers._CHOICE_STATE == {MODEL: "auto"}
        return
    assert providers._CHOICE_STATE == {}
    assert providers._STRICT_STATE == {}
    if case in ("failed_retry", "second_refusal"):
        _assert_forced_strict_follow_up(method)


def test_anthropic_cancelled_first_probe_clears_its_marks():
    seen: list[dict] = []

    async def main():
        hold = asyncio.Event()

        async def handler(request):
            seen.append(json.loads(request.content))
            if len(seen) == 1:
                await hold.wait()
            return httpx.Response(200, json=_tool_payload("choose", {"reason": "r", "choice": "coolant_level_low"}))

        provider = AnthropicProvider("k", MODEL, transport=httpx.MockTransport(handler))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(provider.choose(_request()), 0.2)
        assert await provider.choose(_request()) == ("coolant_level_low", "r")

    asyncio.run(main())
    assert len(seen) == 2
    assert seen[1]["tool_choice"] == {"type": "tool", "name": "choose"}
    assert seen[1]["tools"][0]["strict"] is True
    assert providers._CHOICE_PROBE == set()
    assert providers._STRICT_PROBE == set()


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_anthropic_one_400_naming_both_drops_both_in_one_retry(method):
    good, call, ok = _call_method(method)
    seen: list[dict] = []
    both = (400, _error("strict mode and tool_choice are not supported"))
    assert call(AnthropicProvider("k", MODEL, transport=_sequence(both, ok, seen=seen))) == _answer(method, good)
    assert len(seen) == 2
    assert "strict" not in seen[1]["tools"][0]
    assert seen[1]["tool_choice"] == {"type": "auto"}
    assert providers._STRICT_STATE[MODEL] == "refused"
    assert providers._CHOICE_STATE[MODEL] == "auto"


@pytest.mark.parametrize("method", ["choose", "choose_many"])
def test_tool_only_instruction_is_anthropic_only(method):
    assert providers.TOOL_ONLY_INSTRUCTION == "Answer only by calling the tool, never in plain text."
    good, call = _good(method)
    seen: list[httpx.Request] = []
    call(OpenAIProvider("k", MODEL, transport=_mock(200, _openai_payload(good), seen)))
    system = CHOICE_SYSTEM if method == "choose" else FLAGS_SYSTEM
    assert json.loads(seen[0].content)["messages"][0] == {"role": "system", "content": system}
