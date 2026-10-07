import asyncio
import collections
import json
import os
import tempfile
import threading
import time

import httpx
import pytest

import app.luna as luna
from app import providers
from app.providers import ASK, FakeProvider, Option, ProviderError

LP_EN = "coolant warning came on after the engine control unit was updated"
OPTIONS = [
    Option("coolant_level_low", "Coolant level too low", "Coolant level too low: low coolant.", ("coolant",)),
    Option("engine_control_petrol", "Petrol engine control faulty", "Petrol engine control faulty: fault.",
           ("engine control",)),
]
FLAG_IDS = ["repeat_problem", "recent_long_service", "near_service_interval", "fresh_handover",
            "no_pre_delivery_test"]
FLAG_OPTIONS = [Option(f, f, f"text for {f}") for f in FLAG_IDS]
MAGIC = "MAGICSTRING" + "_PII_" + "ZX9"


class Scripted:
    name = "scripted"

    def __init__(self, choose=("ask", "r"), many=([], "r"), delay=0.0):
        self.answers = {"choose": choose, "choose_many": many}
        self.delay = delay
        self.calls = []

    async def _answer(self, method, request):
        self.calls.append((method, request))
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = self.answers[method]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def choose(self, request):
        return await self._answer("choose", request)

    async def choose_many(self, request):
        return await self._answer("choose_many", request)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv("LUNA_MODE", "on")
    monkeypatch.delenv("LUNA_TOTAL_BUDGET_S", raising=False)


def _choose(provider, options=OPTIONS, context="c"):
    return asyncio.run(luna.choose("q", context, options, provider))


def _many(provider, options=FLAG_OPTIONS, context="c"):
    return asyncio.run(luna.choose_many("q", context, options, provider))


@pytest.mark.parametrize("mode", [None, "off", "codex", "", "true"])
def test_off_returns_none_and_creates_no_provider(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("LUNA_MODE", raising=False)
    else:
        monkeypatch.setenv("LUNA_MODE", mode)
    made = []
    monkeypatch.setattr(providers, "get_provider", lambda *a, **k: made.append(1))
    assert asyncio.run(luna.choose("q", "c", OPTIONS)) is None
    assert asyncio.run(luna.choose_many("q", "c", FLAG_OPTIONS)) is None
    assert luna.select("q", "c", OPTIONS) is None
    assert luna.select_many("q", "c", FLAG_OPTIONS) is None
    assert made == []


@pytest.mark.usefixtures("on")
def test_pick_and_ask_with_a_scripted_provider():
    assert _choose(Scripted(choose=("coolant_level_low", "r"))) == luna.Outcome("picked", "coolant_level_low", "r", False)
    assert _choose(Scripted(choose=("ask", "r"))) == luna.Outcome("asked", None, "r", False)


@pytest.mark.usefixtures("on")
@pytest.mark.parametrize("choice", ["esc_active", "ASK", "Ask", " ask", "coolant_level_low "])
def test_an_id_outside_the_options_is_rejected(choice):
    assert _choose(Scripted(choose=(choice, "r"))) == luna.Outcome("rejected")


@pytest.mark.usefixtures("on")
@pytest.mark.parametrize("answer", [
    None, 5, ("coolant_level_low",), ("coolant_level_low", "r", "x"), (None, "r"), (5, "r"),
    (["coolant_level_low"], "r"), ("coolant_level_low", None), ("coolant_level_low", 5), "coolant_level_low",
])
def test_malformed_answers_are_rejected(answer):
    assert _choose(Scripted(choose=answer)) == luna.Outcome("rejected")


@pytest.mark.usefixtures("on")
def test_choose_many_validation():
    assert _many(Scripted(many=(["repeat_problem"], "r"))) == luna.ManyOutcome("chosen", ("repeat_problem",), "r")
    assert _many(Scripted(many=([], "r"))) == luna.ManyOutcome("none", (), "r")
    twice = _many(Scripted(many=(["repeat_problem", "repeat_problem"], "r")))
    assert twice.status == "chosen" and twice.choices == ("repeat_problem",)
    for answer in ((["all_clear"], "r"), ("repeat_problem", "r"), ([5], "r"), (["repeat_problem"], None), None,
                   (["repeat_problem"], "r", "x")):
        assert _many(Scripted(many=answer)) == luna.ManyOutcome("rejected"), answer


class _NoMethods:
    name = "scripted"


@pytest.mark.usefixtures("on")
@pytest.mark.parametrize("error", [ProviderError("down"), RuntimeError("x"), ValueError("x"), KeyError("x"), None])
def test_provider_failures_never_raise(error):
    provider = _NoMethods() if error is None else Scripted(choose=error, many=error)
    assert _choose(provider) == luna.Outcome("failed")
    assert _many(provider) == luna.ManyOutcome("failed")


_GOOD = {"choose": {"reason": "r", "choice": "coolant_level_low"},
         "choose_many": {"reason": "r", "choices": ["repeat_problem"]}}


def _unsuccessful(case, method):
    good = _GOOD[method]
    block = {"type": "tool_use", "id": "t1", "name": method, "input": good}
    choice = {"finish_reason": "stop", "message": {"content": json.dumps(good)}}
    return {
        "anthropic_refusal": {"content": [block], "stop_reason": "refusal"},
        "anthropic_no_stop_reason": {"content": [block]},
        "anthropic_extra_tool_use": {"content": [block, {"type": "tool_use", "id": "t2", "name": "other", "input": {}}],
                                     "stop_reason": "tool_use"},
        "openai_content_filter": {"choices": [{**choice, "finish_reason": "content_filter"}]},
        "openai_no_finish_reason": {"choices": [{"message": choice["message"]}]},
        "openai_two_choices": {"choices": [choice, choice]},
    }[case]


@pytest.mark.usefixtures("on")
@pytest.mark.parametrize("case", ["anthropic_refusal", "anthropic_no_stop_reason", "anthropic_extra_tool_use",
                                  "openai_content_filter", "openai_no_finish_reason", "openai_two_choices"])
def test_an_unsuccessful_provider_envelope_is_a_failed_outcome(monkeypatch, case):
    monkeypatch.setattr(providers, "_STRICT_STATE", {})
    monkeypatch.setattr(providers, "_STRICT_PROBE", set())
    cls = providers.AnthropicProvider if case.startswith("anthropic") else providers.OpenAIProvider

    def provider(method):
        payload = _unsuccessful(case, method)
        return cls("k", "test-model", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))

    assert _choose(provider("choose")) == luna.Outcome("failed")
    assert _many(provider("choose_many")) == luna.ManyOutcome("failed")


@pytest.mark.usefixtures("on")
def test_the_time_budget_ends_a_stalled_call(monkeypatch):
    monkeypatch.setenv("LUNA_TOTAL_BUDGET_S", "0.2")
    started = time.monotonic()
    assert _choose(Scripted(delay=3600)) == luna.Outcome("failed")
    assert time.monotonic() - started < 2


@pytest.mark.usefixtures("on")
def test_budget_is_finite_and_capped(monkeypatch):
    for raw in (None, "abc", "nan", "inf", "-inf", "1e309"):
        if raw is None:
            monkeypatch.delenv("LUNA_TOTAL_BUDGET_S", raising=False)
        else:
            monkeypatch.setenv("LUNA_TOTAL_BUDGET_S", raw)
        assert luna.budget_s() == 10.0, raw
    monkeypatch.setenv("LUNA_TOTAL_BUDGET_S", "2.5")
    assert luna.budget_s() == 2.5
    for raw in ("31", "1000"):
        monkeypatch.setenv("LUNA_TOTAL_BUDGET_S", raw)
        assert luna.budget_s() == 30.0, raw
    for raw in ("0", "-1"):
        monkeypatch.setenv("LUNA_TOTAL_BUDGET_S", raw)
        provider = Scripted(choose=("coolant_level_low", "r"), many=(["repeat_problem"], "r"))
        assert _choose(provider) == luna.Outcome("failed")
        assert _many(provider) == luna.ManyOutcome("failed")
        assert provider.calls == [], raw


@pytest.mark.usefixtures("on")
def test_bad_option_lists_make_no_call():
    coolant, engine = OPTIONS
    for options in ([], [coolant], [coolant, coolant], [coolant, Option(ASK, "a", "a")],
                    [coolant, Option("", "e", "e")], [coolant, Option(5, "e", "e")]):
        provider = Scripted(choose=("coolant_level_low", "r"))
        assert _choose(provider, options) == luna.Outcome("failed"), options
        assert provider.calls == []
    for options in ([], [FLAG_OPTIONS[0], FLAG_OPTIONS[0]], [Option(ASK, "a", "a")], [Option("", "e", "e")]):
        provider = Scripted(many=(["repeat_problem"], "r"))
        assert _many(provider, options) == luna.ManyOutcome("failed"), options
        assert provider.calls == []


def _fullwidth(text):
    return "".join(chr(ord(c) + 0xFEE0) for c in text)


@pytest.mark.usefixtures("on")
def test_reasoning_is_guarded_and_cut():
    def both(reason):
        picked = _choose(Scripted(choose=("coolant_level_low", reason)))
        chosen = _many(Scripted(many=(["repeat_problem"], reason)))
        return [(picked.reasoning, picked.withheld), (chosen.reasoning, chosen.withheld)]

    assert both("a" * 1000) == [("a" * 300, False)] * 2
    assert both("safe to drive") == [("", True)] * 2
    assert both(_fullwidth("drivable")) == [("", True)] * 2
    assert both("") == [("", False)] * 2
    for reason in ("\ufb01" * 300, "\ufb03" * 300):
        for reasoning, withheld in both(reason):
            assert not withheld and len(reasoning) == 300


@pytest.mark.usefixtures("on")
def test_missing_key_is_a_failed_choice(monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert luna.select("q", "c", OPTIONS) == luna.Outcome("failed")
    assert luna.select_many("q", "c", FLAG_OPTIONS) == luna.ManyOutcome("failed")


@pytest.mark.usefixtures("on")
def test_select_runs_the_fake_provider_with_no_network(monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")

    def no_network(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    outcome = luna.select("q", LP_EN, OPTIONS)
    assert outcome.status == "picked" and outcome.choice == "coolant_level_low"


@pytest.mark.usefixtures("on")
def test_no_request_content_is_logged(caplog):
    context = f"{MAGIC} {LP_EN}"
    with caplog.at_level("DEBUG"):
        for provider in (Scripted(choose=("esc_active", MAGIC), many=(["x"], MAGIC)),
                         Scripted(choose=RuntimeError(MAGIC), many=RuntimeError(MAGIC)),
                         Scripted(choose=("coolant_level_low", MAGIC), many=(["repeat_problem"], MAGIC)),
                         Scripted(choose=(MAGIC,), many=None)):
            _choose(provider, context=context)
            _many(provider, context=context)
    assert caplog.records, "nothing was logged, so this test proves nothing"
    assert not [r for r in caplog.records if MAGIC in r.getMessage()]


# Phone-shaped text in these cases uses 555-0100 to 555-0199, the range set aside for fiction (knowledge/customers.json).
@pytest.mark.parametrize("secret", ["12345", "someone@example.com", "+1 (555) 012-3"])
def test_mask_pii_each_pattern(secret):
    masked = luna.mask_pii(f"light on, {secret} thanks")
    assert secret not in masked and "[REDACTED]" in masked, masked


@pytest.mark.usefixtures("on")
def test_select_never_creates_temp_dirs(monkeypatch):
    monkeypatch.setenv("CARMIND_PROVIDER", "fake")
    tmp_dir = tempfile.gettempdir()
    before = set(os.listdir(tmp_dir))
    assert luna.select("q", LP_EN, OPTIONS).status == "picked"
    assert luna.select_many("q", "check engine light came on again", FLAG_OPTIONS).status == "chosen"
    assert set(os.listdir(tmp_dir)) - before == set()


@pytest.mark.usefixtures("on")
def test_cancellation_reaches_the_stalled_provider(monkeypatch):
    monkeypatch.setenv("LUNA_TOTAL_BUDGET_S", "0.2")
    cancelled = []

    class Stalled:
        name = "scripted"

        async def choose(self, request):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    assert _choose(Stalled()) == luna.Outcome("failed")
    assert cancelled == [True]


@pytest.mark.usefixtures("on")
def test_outside_cancellation_propagates():
    async def main():
        task = asyncio.create_task(luna.choose("q", "c", OPTIONS, Scripted(delay=3600)))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert luna._REMOTE_SLOTS._value == 8


def _threads(count, fn):
    barrier = threading.Barrier(count)
    results = [None] * count

    def run(i):
        barrier.wait()
        started = time.monotonic()
        outcome = fn()
        results[i] = (outcome, time.monotonic() - started)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


@pytest.mark.usefixtures("on")
def test_at_most_8_remote_choices_at_a_time(monkeypatch):
    provider = Scripted(choose=("ask", "r"), delay=1.0)
    monkeypatch.setattr(providers, "get_provider", lambda *a, **k: provider)
    results = _threads(9, lambda: luna.select("q", "c", OPTIONS))
    assert collections.Counter(o.status for o, _ in results) == {"asked": 8, "failed": 1}
    assert len(provider.calls) == 8
    assert [elapsed for o, elapsed in results if o.status == "failed"][0] < 0.5


@pytest.mark.usefixtures("on")
def test_the_fake_provider_is_not_capped(monkeypatch):
    class SlowFake(FakeProvider):
        async def choose(self, request):
            await asyncio.sleep(0.5)
            return await super().choose(request)

    monkeypatch.setattr(providers, "get_provider", lambda *a, **k: SlowFake())
    results = _threads(40, lambda: luna.select("q", LP_EN, OPTIONS))
    assert [o.status for o, _ in results] == ["picked"] * 40


@pytest.mark.usefixtures("on")
def test_each_call_holds_one_slot():
    entered, release = None, None

    class Held:
        name = "scripted"

        async def choose_many(self, request):
            entered.set()
            await release.wait()
            return [], "r"

    async def main():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(luna.choose_many("q", "c", FLAG_OPTIONS, Held()))
        await entered.wait()
        during = luna._REMOTE_SLOTS._value
        release.set()
        outcome = await task
        return during, outcome

    during, outcome = asyncio.run(main())
    assert during == 7
    assert outcome.status == "none"
    assert luna._REMOTE_SLOTS._value == 8


@pytest.mark.usefixtures("on")
def test_one_budget_is_shared_by_both_calls(monkeypatch):
    from app.models import ChatRequest
    from app.orchestrator import answer

    monkeypatch.setenv("LUNA_TOTAL_BUDGET_S", "0.2")
    calls = {"choose": [], "choose_many": []}

    class Slow:
        name = "scripted"

        async def choose_many(self, request):
            calls["choose_many"].append(request)
            await asyncio.sleep(0.15)
            return [], "r"

        async def choose(self, request):
            calls["choose"].append(request)
            await asyncio.sleep(0.15)
            return "ask", "r"

    monkeypatch.setattr(providers, "get_provider", lambda *a, **k: Slow())
    started = time.monotonic()
    reply = answer(ChatRequest(language="en-US", plate="FH-104-27",
                               messages=[{"role": "user", "content": "coolant light and check engine light both on"}]))
    elapsed = time.monotonic() - started
    assert len(calls["choose_many"]) == 1
    assert len(calls["choose"]) in (0, 1)
    assert "A language model" not in reply
    assert elapsed < 0.5
