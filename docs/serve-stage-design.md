# Serve stage design

The serve stage is the part of carmind that answers questions at request time. It is meant
to sit behind any chat assistant that call-center staff use. This note records the design
the serve code is built toward; "Where the build stands" below says which parts exist.

## Endpoint contract

carmind defines its own small contract, so any chat client can call it with plain JSON:

- Request: `POST` to `/`, `/assistant` or `/chat` with a JSON body `{language, messages}`,
  where each message is `{role, content}`. A Bearer token may be sent; it is not checked.
- Response: `200` with the JSON body `{"reply": "..."}`, also when a request cannot be
  answered: an internal error in carmind's own code or data gets a reply that escalates the call,
  never a 5xx status.

## Layers

1. **HTTP endpoint.** A thin layer any chat client calls. It parses the request, hands it on
   and returns the reply in the contract's shape.
2. **Knowledge tools.** carmind's knowledge as deterministic tools, meant to be served over
   MCP so a model can call them:
   - `identify_vehicle`
   - `lookup_indicator`
   - `get_fleet_procedure`
   - `triage_checklist`
   - `drivability_assessment`
   - `render_form`
3. **Orchestrator.** An LLM agent that reads the latest user turn, calls the tools
   and writes the reply in the user's language. The reply is layered: the fleet's own
   rulebook entry comes first, then carmind's assessment.

## Vertical slice

The first slice covers one platform cell: Skoda petrol, with its six reference
indicator records, plus the fleet rulebook. Its purpose is to show the full path working:
a chat client calls the endpoint, and the endpoint answers from the real knowledge base.

The tools and the endpoint are deterministic, so they are testable from the start. The
LLM orchestration is added behind them and does not change what they return.

## Build order

1. The serve vertical slice: the tool layer and the HTTP endpoint on the Skoda petrol
   cell, with an LLM wired in as the orchestrator and the deterministic tools under test
   from day one.
2. Extraction by the LLM and rule-based verification, checked for regressions against the
   six reference records.
3. A way for a human to sign records off.
4. Extraction widened to further platform cells, then translation, then a wider serve
   knowledge base.

## Where the build stands

In the current code (details in `serve/README.md`):

- The contract endpoint is built. `POST /`, `/assistant` and `/chat` answer with
  `{"reply": ...}` from the deterministic orchestrator in `serve/app/orchestrator.py`:
  keyword match, record lookup, then a reply built from templates in `serve/app/compose.py`.
  An optional `context_flags` list can only tighten a verdict.
  Optional `caller_number` and `plate` fields identify the vehicle; a number in
  the message text is never read as a plate or a caller number.
- An MCP server is built. `serve/app/mcp_server.py` serves four deterministic, read-only
  tools from `serve/app/desk.py` over streamable HTTP on port 7601, path `/mcp`:
  `get_customer`, `assess_warning_light`, `get_company_procedure` and `plan_dispatch`. It
  validates the knowledge base before it serves.
- An agent loop is built. `POST /api/agent` runs `run_agent` in `serve/app/agent.py`: the
  model gets up to five turns, at most six tool calls run per turn, and the whole turn has
  one 60-second deadline. The tools are called over MCP when `CARMIND_MCP_URL` is set and
  in-process otherwise. The customer id, the caller number, the plate number, the problem
  text and the after-hours flag are forced onto every tool call that takes them, and a guard runs `get_customer`, `assess_warning_light` and
  `plan_dispatch` when the model skipped them or they failed.
- Constrained choice (luna) is built in `serve/app/luna.py`. With `LUNA_MODE=on` a model
  answers up to two questions per request from closed lists sent as enums in the
  provider's structured output, with the reason asked for before the ids and the sets
  enforced by code: which listed car-history facts the description states, asked only when
  a resolved drivable light could be tightened by a context rule, none has fired yet, and
  the request carries no flags other than after_service_hours (the answer is added to the
  flags and can only make the verdict more cautious), and which of several matched
  drivable lights the caller means (any of them, or `ask`). An accepted pick is rendered
  from that light's record, and the reply names every matched light. On the contract
  endpoint the reply names the inferred flags and the words behind the pick, written by
  code; on `/api/agent` the pick is forced onto `assess_warning_light` before the model's
  turn, the assessment and dispatch the reply shows are computed in-process after the
  guard with the inferred flags, the dispatch is the more cautious of the plans with and
  without the pick, and the model's filtered reasons are shown. Both questions share one
  time budget. It calls none of carmind's tools itself.
- Three model providers are built in `serve/app/providers.py`, chosen by
  `CARMIND_PROVIDER`: `fake` (the default, no network), `anthropic` and `openai`. The fake
  provider calls the three screen tools, then `get_company_procedure` for the first
  procedure the `plan_dispatch` result names, then writes a fixed note. The Anthropic and
  OpenAI providers read their key and model id from the environment only.
- The verdict and the dispatch are rendered by code. The reply ends with a
  `carmind verdict:` line, which is the `verdict_text` of `assess_warning_light`, and a
  `carmind dispatch:` line built from the fields of `plan_dispatch`.
- The model's text is a guarded note. It is shown only under the label "Model note (not a
  verdict)", and it is withheld when, after normalisation, it contains a verdict,
  drivability, severity or dispatch stem in English, Hebrew or Arabic, a rendered verdict
  label, or a character outside ASCII, Hebrew and Arabic.
- Failures fall back to the deterministic tools. When the provider is not set up or
  returns an error, the reply says the language model is not available, and the verdict
  and dispatch still come from the tools, without any light choice. When the turn fails in
  any other way, the deadline or a dead MCP service included, the tools run in-process and
  the reply carries no model note and no light choice. Inferred context flags, which can
  only tighten, are kept in both cases.
- The operator page is built. `GET /` serves `serve/app/static/index.html` and `app.js`, and
  `serve/app/ui.py` serves `GET /api/config` (every string, colour, language and limit the page
  shows), the script and 14 bundled fonts, with a Content-Security-Policy that allows nothing
  from outside. A conversation keeps one caller and plate, and each question is its own
  `POST /api/agent`. The page shows the reply as written by code, with the verdict badge and a
  Details row. Replies are in English; the language setting chooses the dictation language and
  the layout direction. Dictation works only where the browser offers on-device recognition, and
  read-aloud uses only on-device voices. Conversations can be kept in the browser's IndexedDB and
  saved to files, encrypted with AES-256-GCM under a PBKDF2-SHA256 key from the user's
  passphrase; the service stores none of it.
- Not built yet: `triage_checklist`, `render_form` and `drivability_gate` in
  `serve/app/tools.py` raise `NotImplementedError`.
