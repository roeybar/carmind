"""
Constrained choice ("luna"): a language model picks from a closed list. Off unless LUNA_MODE=on. Two calls use it:
which car-history context flags a description states (any number, or none) and which of several matched lights the
caller means (one, or "ask"). Code builds the questions and the options; the provider's structured output asks for a
short reason, then the ids. Code rejects any other answer and filters the reason like the agent's note. Both calls of
a request share one time budget, and at most 8 calls wait on a remote provider at a time. A provider error, a
malformed or rejected answer, a busy slot or the time budget gives an outcome the caller treats as no answer; none of
these raises, while a cancellation from outside still propagates.
"""
import asyncio
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass

from app import providers
from app.noteguard import WITHHELD_NOTE, guard_note
from app.providers import ASK, ChoiceRequest, Option

logger = logging.getLogger(__name__)

REASON_MAX_CHARS = 300
DEFAULT_BUDGET_S = 10.0
MAX_BUDGET_S = 30.0
MAX_REMOTE_CHOICES = 8
_REMOTE_SLOTS = threading.BoundedSemaphore(MAX_REMOTE_CHOICES)
_FAILED = object()

_DIGIT_RUN = re.compile(r'\b\d{5,}\b')
_EMAIL_SHAPE = re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b')
_PHONE_SHAPE = re.compile(r'\+?[\d\s\-()]{8,}')


def mask_pii(text: str) -> str:
    """Replace digit runs of 5+, email-like and phone-like strings with [REDACTED]."""
    text = _DIGIT_RUN.sub("[REDACTED]", text)
    text = _EMAIL_SHAPE.sub("[REDACTED]", text)
    text = _PHONE_SHAPE.sub("[REDACTED]", text)
    return text


@dataclass(frozen=True)
class Outcome:
    status: str  # "picked" | "asked" | "rejected" | "failed"
    choice: str | None = None
    reasoning: str = ""
    withheld: bool = False


@dataclass(frozen=True)
class ManyOutcome:
    status: str  # "chosen" | "none" | "rejected" | "failed"
    choices: tuple[str, ...] = ()
    reasoning: str = ""
    withheld: bool = False


def enabled() -> bool:
    return (os.environ.get("LUNA_MODE") or "off").strip().lower() == "on"


def budget_s() -> float:
    raw = os.environ.get("LUNA_TOTAL_BUDGET_S", "")
    try:
        value = float(raw) if raw.strip() else DEFAULT_BUDGET_S
    except (ValueError, OverflowError):
        return DEFAULT_BUDGET_S
    if not math.isfinite(value):
        return DEFAULT_BUDGET_S
    return min(value, MAX_BUDGET_S)


def request_deadline() -> float:
    """The monotonic time by which every luna call of one request must end."""
    return time.monotonic() + budget_s()


def _valid_ids(options: list[Option], minimum: int) -> list[str] | None:
    ids = [o.id for o in options]
    if (len(ids) < minimum or len(set(ids)) != len(ids) or ASK in ids
            or not all(isinstance(i, str) and i for i in ids)):
        return None
    return ids


async def _ask(method: str, request: ChoiceRequest, provider, deadline: float | None):
    """The provider's raw answer, or _FAILED (logged by type). One slot per call, held only during the call."""
    remaining = (request_deadline() if deadline is None else deadline) - time.monotonic()
    if remaining <= 0:
        return _FAILED
    try:
        if provider is None:
            provider = providers.get_provider()
    except Exception as exc:
        logger.error("luna call failed: %s", type(exc).__name__)
        return _FAILED
    remote = getattr(provider, "name", None) != "fake"
    if remote and not _REMOTE_SLOTS.acquire(blocking=False):
        logger.warning("luna busy: %d calls already waiting", MAX_REMOTE_CHOICES)
        return _FAILED
    try:
        async with asyncio.timeout(min(remaining, MAX_BUDGET_S)):
            return await getattr(provider, method)(request)
    except Exception as exc:
        logger.error("luna call failed: %s", type(exc).__name__)
        return _FAILED
    finally:
        if remote:
            _REMOTE_SLOTS.release()


def _guarded(reason: str) -> tuple[str, bool]:
    shown = guard_note(reason[:REASON_MAX_CHARS])
    if shown == WITHHELD_NOTE:
        return "", True
    return shown[:REASON_MAX_CHARS], False


async def choose(question, context, options, provider=None, deadline=None) -> Outcome | None:
    """None when LUNA_MODE is not on; otherwise an Outcome. Provider and answer errors never raise."""
    if not enabled():
        return None
    ids = _valid_ids(options, 2)
    if ids is None:
        return Outcome("failed")
    raw = await _ask("choose", ChoiceRequest(question, context, tuple(options)), provider, deadline)
    if raw is _FAILED:
        return Outcome("failed")
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        logger.warning("luna answer rejected: not a choice and a reason")
        return Outcome("rejected")
    choice, reason = raw
    if not isinstance(choice, str) or (choice not in ids and choice != ASK) or not isinstance(reason, str):
        logger.warning("luna answer rejected: not one of the options")
        return Outcome("rejected")
    reasoning, withheld = _guarded(reason)
    return Outcome("asked", None, reasoning, withheld) if choice == ASK else Outcome("picked", choice, reasoning, withheld)


async def choose_many(question, context, options, provider=None, deadline=None) -> ManyOutcome | None:
    if not enabled():
        return None
    ids = _valid_ids(options, 1)
    if ids is None:
        return ManyOutcome("failed")
    raw = await _ask("choose_many", ChoiceRequest(question, context, tuple(options)), provider, deadline)
    if raw is _FAILED:
        return ManyOutcome("failed")
    if not isinstance(raw, (tuple, list)) or len(raw) != 2:
        logger.warning("luna answer rejected: not choices and a reason")
        return ManyOutcome("rejected")
    choices, reason = raw
    if (not isinstance(choices, (list, tuple)) or not all(isinstance(c, str) and c in ids for c in choices)
            or not isinstance(reason, str)):
        logger.warning("luna answer rejected: not among the options")
        return ManyOutcome("rejected")
    picked = tuple(i for i in ids if i in set(choices))
    reasoning, withheld = _guarded(reason)
    return ManyOutcome("chosen" if picked else "none", picked, reasoning, withheld)


def select(question, context, options, deadline=None) -> Outcome | None:
    if not enabled():
        return None
    try:
        return asyncio.run(choose(question, context, options, deadline=deadline))
    except Exception as exc:
        logger.error("luna call failed: %s", type(exc).__name__)
        return Outcome("failed")


def select_many(question, context, options, deadline=None) -> ManyOutcome | None:
    if not enabled():
        return None
    try:
        return asyncio.run(choose_many(question, context, options, deadline=deadline))
    except Exception as exc:
        logger.error("luna call failed: %s", type(exc).__name__)
        return ManyOutcome("failed")
