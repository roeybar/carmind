"""
FastAPI entry point: POST /, /assistant and /chat return {"reply": ...}.

The demo adds GET / (the operator page), GET /api/customers and POST /api/agent, which runs
the call-desk agent over the carmind tools, and GET /api/config, /app.js and /fonts/ (app/ui.py).

An error in carmind's own code or data on the reply routes returns 200 with a conservative escalate reply instead of a 5xx.
A bearer token is accepted and ignored. The knowledge base is loaded and validated at
startup; if it does not match the schema, the service does not start.
"""
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import get_args

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app import agent, desk, kb, providers, ui
from app.compose import compose_escalate, stop_precaution
from app.models import AgentRequest, ChatRequest, ContextFlag
from app.orchestrator import answer, stop_warning_or_fallback
from app.tools import field_given

logger = logging.getLogger(__name__)

# A short tag on the service's own error lines, so they can be picked out of a shared log.
LOG_TAG = "220a2b39"

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    kb.indicators()
    yield


app = FastAPI(title="carmind-serve", version="1.0.0", lifespan=lifespan)
app.include_router(ui.router)


async def _off_loop(fn, *args):
    """fn(*args) in the thread pool, or inline when the thread pool call raises. Error paths only."""
    try:
        return await run_in_threadpool(fn, *args)
    except Exception as exc:
        logger.error("%s thread pool call failed: %s", LOG_TAG, type(exc).__name__)
        return fn(*args)


async def _handle(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        req = ChatRequest.model_validate(body)
        reply = await run_in_threadpool(answer, req)
        return JSONResponse({"reply": reply})
    except Exception as exc:
        logger.error("%s request handling error: %s", LOG_TAG, type(exc).__name__)
        try:
            parsed = json.loads(await request.body())
        except Exception:
            parsed = None
        try:
            reply = await _off_loop(_error_reply, parsed)
        except Exception as exc:
            logger.error("%s error reply failed: %s", LOG_TAG, type(exc).__name__)
            reply = _static_error_reply(parsed)
        return JSONResponse({"reply": reply})


def _read_body(parsed) -> tuple:
    """The language, last user message and known context flags of a request body; defaults when unreadable."""
    lang, text, flags = "he-IL", None, []
    try:
        body = parsed if isinstance(parsed, dict) else {}
        candidate = body.get("language")
        if isinstance(candidate, str) and candidate:
            lang = candidate
        messages = body.get("messages")
        contents = [m["content"] for m in messages if isinstance(m, dict) and m.get("role") == "user"
                    and isinstance(m.get("content"), str)] if isinstance(messages, list) else []
        text = contents[-1] if contents else None
        raw_flags = body.get("context_flags")
        known = get_args(ContextFlag)
        flags = [f for f in raw_flags if isinstance(f, str) and f in known] if isinstance(raw_flags, list) else []
    except Exception:
        logger.error("%s error reply: unreadable request body", LOG_TAG)
    return lang, text, flags


def _error_reply(parsed) -> str:
    """The error reply in the request language, with the do-not-drive line when the last user message describes a stop light."""
    lang, text, flags = _read_body(parsed)
    reply = compose_escalate(lang, "error")
    if text is not None and stop_warning_or_fallback(text, flags):
        reply += "\n\n" + stop_precaution(lang)
    return reply


def _static_error_reply(parsed) -> str:
    """The last-resort error reply: the precaution whenever the last user message is a non-empty string."""
    lang, text, _flags = _read_body(parsed)
    reply = compose_escalate(lang, "error")
    if isinstance(text, str) and text:
        reply += "\n\n" + stop_precaution(lang)
    return reply


@app.post("/")
async def root(request: Request) -> JSONResponse:
    return await _handle(request)


@app.post("/assistant")
async def assistant(request: Request) -> JSONResponse:
    return await _handle(request)


@app.post("/chat")
async def chat(request: Request) -> JSONResponse:
    return await _handle(request)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers=ui.PAGE_HEADERS)


@app.get("/api/customers")
def customers() -> dict:
    return {"customers": [
        {"id": c["id"], "name": c["name"], "phone": c["phone"],
         "vehicles": [{"plate": v["plate"], "car": desk.car_label(v)} for v in c["vehicles"]],
         "coverage": c["coverage"], "location": c["location"]["description"], "zone": c["location"]["zone"]}
        for c in kb.customers()
    ]}


def _rejection(error: str, body) -> dict:
    """A 400 body; the do-not-drive precaution is added when the problem describes a stop light."""
    out = {"error": error}
    problem = body.get("problem") if isinstance(body, dict) else None
    if isinstance(problem, str):
        flags = ["after_service_hours"] if body.get("after_hours") is True else []
        if stop_warning_or_fallback(problem, flags):
            out["precaution"] = stop_precaution("en-US")
    return out


async def _rejected(error: str, body) -> JSONResponse:
    try:
        out = await _off_loop(_rejection, error, body)
    except Exception as exc:
        logger.error("%s rejection reply failed: %s", LOG_TAG, type(exc).__name__)
        out = {"error": error}
        problem = body.get("problem") if isinstance(body, dict) else None
        if isinstance(problem, str) and problem:
            out["precaution"] = stop_precaution("en-US")
    return JSONResponse(out, status_code=400)


@app.post("/api/agent")
async def agent_route(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    try:
        req = AgentRequest.model_validate(body)
    except Exception:
        return await _rejected("invalid request", body)
    try:
        cid = req.customer_id
        if field_given(cid) and (not isinstance(cid, str) or len(cid) > 40 or kb.customer(cid) is None):
            return await _rejected("unknown customer", body)
        try:
            provider, setup_error = providers.get_provider(), None
        except providers.ProviderError as exc:
            provider, setup_error = None, str(exc)
        result = await agent.run_agent(req.customer_id, req.problem, req.after_hours, provider,
                                       agent.tools_from_env(), setup_error, req.caller_number, req.plate)
        return JSONResponse(result)
    except Exception as exc:
        logger.error("%s agent error: %s", LOG_TAG, type(exc).__name__)
        text, status = compose_escalate("en-US", "error"), "unclear"
        flags = ["after_service_hours"] if req.after_hours else []
        try:
            stop = await _off_loop(stop_warning_or_fallback, req.problem, flags)
        except Exception as exc:
            logger.error("%s stop check failed: %s", LOG_TAG, type(exc).__name__)
            stop = isinstance(req.problem, str) and req.problem != ""
        if stop:
            text, status = text + "\n\n" + stop_precaution("en-US"), "not_drivable"
        return JSONResponse({
            "error": "internal error", "reply": text,
            "verdict": {"status": status, "severity": None, "indicator": None, "text": text, "source": None},
            "dispatch": None, "note": "", "tool_calls": [], "provider": None, "customer": None, "choice": None, "context": None,
        })
