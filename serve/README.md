# carmind-serve

FastAPI service that answers dashboard-light questions for the call-center assistant, plus a
one-page operator demo whose agent calls four deterministic carmind tools over MCP.
See the top-level README for the project overview.

## Demo

From the repository root:

```
docker compose up --build -d
```

Open http://127.0.0.1:7600/. The menu button in the header opens the drawer: pick a demo caller (it
fills the caller number), add a plate number if the caller has more than one car, or pick an example.
Describe the warning light in the box at the bottom and press the send button ("Ask the agent") or
Enter. The caller fields lock after the first question; "New" starts a conversation for another
caller. The default provider is `fake` (no network, no key). All customers, places and companies are
invented. No `.env` is needed; copy `.env.example` to `.env` only to use a real provider.

## The operator page

`index.html` and `app.js` make up the page, and `app/ui.py` serves its settings: every string,
colour, language and limit comes from `GET /api/config`. Server text is put on the page as text,
never as HTML. A conversation is one call: each question is assessed on its own with that
conversation's caller and plate, and earlier turns are not sent. Each answer is a bubble with
carmind's reply as written by code and the verdict badge; Details shows the source, the dispatch,
the model note, the context and light choice, the tool calls and the provider. A failed request
shows the fixed precaution "The request failed. If the problem involves the brakes, the steering,
smoke, fire or a red warning light, do not drive the car until it has been checked."; a rejected
one shows the server's error and its precaution.

Language: `en-US` or `he-IL` sets the dictation language and the layout direction (he-IL is right to
left). `/api/agent` replies are in English in both cases, and read-aloud reads them with an on-device
English voice.

Dictation and saves need a secure page: speech recognition and the WebCrypto encryption require one
(IndexedDB itself does not). Open the demo at http://127.0.0.1:7600 or http://localhost:7600 on the
machine that runs it; on an address that is not secure, the page says so and keeps dictation and saves
off. HTTPS serving is not provided.

Speech: dictation works only where the browser offers on-device speech recognition (the Web Speech
API's `available()` and `processLocally`). The page asks for on-device processing, and turns
dictation off with a notice when the browser cannot give it. The first press may first fetch the
browser's on-device model for the language. What you say goes into the problem box and is sent only
when you send it. While you speak, the microphone stream drives the level rings and is never recorded
or uploaded. Read-aloud uses only voices the browser marks as on-device (`localService`); without
one, the page says so. Which browsers and languages offer on-device speech is up to the browser.

Saves: conversations stay in memory until you set a passphrase in the drawer (at least 8
characters, typed twice). From then on each conversation is saved in this browser's IndexedDB
(database `carmind`), encrypted with AES-256-GCM under a key derived from the passphrase by
PBKDF2-SHA256 with 600,000 iterations and a random salt; every save uses a new random IV. The
passphrase itself is not kept, so after a reload you unlock once. If you forget the passphrase, the
saved conversations and saved files cannot be read, and carmind cannot recover it. A passphrase that
opens none of the saved conversations is refused; you can then start new saves under a passphrase
you type twice, and the old ones stay, locked. Nothing is overwritten: if another tab changed a
conversation, this tab saves its copy as a new one. If the browser refuses a save, the page offers
the conversation as a file, and leaving the page with unsaved conversations asks first. The service
never receives the saves.

Files: "Save to file" on a conversation in the drawer downloads it as `carmind-YYYYMMDD-HHMM.json`,
encrypted the same way; the passphrase must be set or unlocked first. "Save to file and remove" (the
x) saves the file first, and removes the conversation from the browser only after the file has been
handed to the browser as a download; it is disabled for the open conversation, while a request is
pending, and on a page that is not secure. "Open a saved
file" reads a file of up to 4 MiB, checks its format, decrypts it with your passphrase (or asks for
the one it was saved with) and checks every field. It then opens the file as a new conversation
marked "imported", never replaces a saved one and, with saves on, saves it like any other. carmind
does not check the answers in an imported file again.

`GET /` sends a Content-Security-Policy (`PAGE_CSP` in `app/ui.py`) that allows scripts, styles,
fonts and requests only from the service itself. In browsers that support Trusted Types, it also
blocks inserting HTML from a string. Besides its own script (`/app.js`) and fonts (`/fonts/{name}`),
the page talks only to `/api/config`, `/api/customers` and `/api/agent`.

## Contract

```
POST /  (also /assistant and /chat)
Body:   {"language": "<bcp47>", "messages": [{"role": "user", "content": "..."}],
         "context_flags": ["repeat_problem", ...], "caller_number": "+1-555-0142", "plate": "FH-104-27"}
         (all optional, language defaults to he-IL; a caller_number or plate that is not a string or is
         over 40 characters counts as not on record)
Header: Authorization: Bearer <token>  (accepted, ignored)
Reply:  {"reply": "<layered text>"}
GET /health -> {"status": "ok"}

GET / -> the operator page (index.html, app.js, bundled fonts; no external assets)
GET /api/config     -> {"product_name", "theme", "languages", "reply_language", "capabilities", "limits",
                        "examples", "badges", "actions", "copy"}: every string, colour, language and
                        limit the page uses
GET /app.js         -> the page's script
GET /fonts/{name}   -> one of the 14 bundled woff2 fonts named in app/ui.py (FONT_FILES); any other
                        name gets 404
GET /api/customers  -> {"customers": [{"id", "name", "phone", "vehicles": [{"plate", "car"}], "coverage", "location", "zone"}]}
POST /api/agent
Body:   {"caller_number": "+1-555-0142", "plate": "", "customer_id": "", "problem": "<up to 4000 chars>", "after_hours": false}
        (caller_number, plate and customer_id optional; null and "" mean not given)
Reply:  {"customer": {...}, "verdict": {"status", "severity", "indicator", "text", "source"},
         "dispatch": {...}, "note": "...", "reply": "...",
         "tool_calls": [{"name", "by": "model|guard", "summary"}], "provider": "fake", "error": null,
         "context": null or {"status": "chosen|none|rejected|failed", "flags": [...], "options": [...],
                             "reasoning": "...", "withheld": false},
         "choice": null or {"status": "picked|asked|rejected|failed", "picked": "<light id>" or null,
                            "options": [{"id", "name"}], "reasoning": "...", "withheld": false}}
        400 {"error": "invalid request"} or {"error": "unknown customer"}, with "precaution": "Do not drive until the car has been checked." added when the problem describes a stop light (a body that is not JSON gets no precaution)
```

`choice` is null when no light choice was made, and after a failed turn; `context` keeps inferred
flags after a failed turn, since they can only tighten.

`verdict.status` is one of `not_drivable`, `drivable_caution`, `drivable`, `unclear`,
`unsupported`.

Languages: `he-IL`, `en-US`, `ar-SA`, `ru-RU`. Arabic and Russian get English section labels;
the rulebook body stays in Hebrew. An error in carmind's own code or data on the reply routes returns 200 with an escalate reply; if the server itself fails (out of memory, a stopped worker), no reply is given.

## The agent route

`POST /api/agent` runs a language model with four tools served by the separate `mcp` service
(`get_customer`, `assess_warning_light`, `get_company_procedure`, `plan_dispatch`). The tools are
deterministic and read-only; their data is `knowledge/customers.json`,
`knowledge/company-policy.json` and `knowledge/dispatch-model.json`. `assess_warning_light` also
takes an optional `light`; see its tool description.

Before the model's turn the app may ask luna two questions (see "Constrained choice (luna)"): which
listed car-history facts the problem states, and which of several matched drivable lights the caller
means. A picked light is forced onto `assess_warning_light` as its `light` argument, like the
request fields; the model's own tool calls see the pick but not the inferred facts. After the guard,
the app computes the assessment and the dispatch the reply shows again, in-process, with the
inferred facts and the pick: the dispatch is then the more cautious of the plan for the picked light
and the plan for an unconfirmed light. If the provider fails during the turn, or the turn fails in
any other way, the pick is dropped and the assessment is computed again without it; inferred facts
are kept, because they can only tighten.

The verdict is always a record's, never the model's words. The reply is built by code: the model's
note (labelled "Model note (not a verdict)", only when it is shown), then the inferred context and the
light choice when luna gave them, then the `carmind verdict:` line, then the
`carmind dispatch:` line (action, provider and whether the plan covers it), both from the tools'
own fields. The customer id, the caller number, the plate number, the problem text and the
after-hours flag are forced onto every tool call that takes them, and any of the three screen tools the model skipped or that failed is run by a guard (a
failed one is re-run in-process, so the verdict never depends on the MCP service). The model's
text is withheld entirely when, after Unicode NFKC normalisation, removing invisible format
characters, lowercasing and collapsing whitespace, it contains a stem about the verdict,
drivability, severity or dispatch (`driv`, `safe`, `sever`, `stop`, `tow`, `operational`,
`verdict`, `caution`, `danger`, `ok to`, `fine to`, `roadworthy`, `continue`, `dispatch`,
`rescue`, `recovery`, `dealer`, `appointment`, `truck`, `roadside`, `booked`, `pull over`,
`carry on`, and Hebrew and Arabic equivalents), or any character outside ASCII, Hebrew and
Arabic. The whole turn runs under one 60 s deadline; on expiry, or on any failure of the turn,
the reply is the deterministic assessment with no model note. The `fake` provider's note is
always "I looked up the customer, the car and the company procedure."

The MCP service listens on `mcp:7601/mcp` inside the compose network and is not published.

### Providers

`CARMIND_PROVIDER` selects the model: `fake` (default), `anthropic` or `openai`.

| Variable | Meaning |
|---|---|
| `CARMIND_PROVIDER` | `fake`, `anthropic` or `openai` |
| `ANTHROPIC_API_KEY`, `CARMIND_ANTHROPIC_MODEL` | required for `anthropic` |
| `OPENAI_API_KEY`, `CARMIND_OPENAI_MODEL` | required for `openai` |
| `CARMIND_MCP_URL` | the MCP endpoint (set by compose); unset runs the tools in-process |
| `LUNA_MODE` | `on` (the demo default) lets a language model infer listed car-history context flags (they can only tighten) and pick one of several matched drivable lights, from closed lists; `off` makes no model call |
| `LUNA_TOTAL_BUDGET_S` | seconds both luna calls of a request may take together (default 10, at most 30); after that there is no answer |
| `CARMIND_ANTHROPIC_STRICT` | `off` sends luna's tools to Anthropic without strict tool use; default on |

Put them in `.env` (git-ignored). Model ids come from the environment only. With a real provider
the problem text goes to that provider, together with the tool results without the customer's phone
number; with `fake` nothing leaves the machine. A missing key or
model does not break the route: the reply carries the deterministic verdict and an `error`.

## Reply layers

**Section A, fleet rulebook.** The fleet's own entry for the matched light, verbatim and
attributed. The rulebook in this repository (`knowledge/example-icon-rulebook.json`) is a
synthetic example.

**Section B, carmind assessment.** Indicator name and meaning, severity, a verdict line and
the manual citation. Severity and hard_stop come unchanged from the indicator record. A
stop_now or hard_stop light always gets the manual's own message and an explicit
do-not-drive line.

## Context flags

`context_flags` is a list of per-car flags from the schema's `context_flag` enum:
`repeat_problem`, `recent_long_service`, `near_service_interval`, `fresh_handover`,
`no_pre_delivery_test`, `after_service_hours`. Any other value gets the error escalate reply.
The flags are used for that request only; they are not written to disk or logged.

A rule in `knowledge/context-rules.json` fires when all of its `when_flags` are present
(and, if it has a `when_symptom`, only for that indicator id). What it does:

- `tow`: a drivable verdict (info, caution, conditional) becomes "Tow recommended - do not
  drive.", followed by a `Context:` line naming the flags.
- `escalate`: a drivable verdict becomes the escalate verdict, with the same `Context:` line.
- `raise_conservatism`: no change to a single-light verdict.
- Any fired rule on a query that matches several lights keeps the LLM out and adds the
  do-not-drive precaution.

A verdict that is already not drivable (stop_now, hard_stop, unknown severity) never
changes, and the severity shown is always the record's own. The shipped file has one rule:
`repeat_problem` -> tow.

## Vehicle resolution

- The vehicle comes from the caller number and the plate number field, never from a number in the
  message text. Both are looked up in `knowledge/customers.json`, where each customer has a phone
  number and one or more cars.
- Caller number: compared by its digits (spaces, dashes, dots, brackets and a leading `+` are
  ignored). A caller on record with one car identifies that car. A customer with several cars gets
  a question listing that customer's cars, and the plate number field picks one.
- Plate number field: read in any case, with or without spaces and dashes (`fh 104 27`, `FH10427`).
  A plate that is not on record gets no assessment, and neither does a plate that is not one of the
  caller's cars.
- On the chat endpoint only, with no caller on record and no plate, the text can name the make, the
  model and the powertrain (`Skoda Octavia petrol`; a mild hybrid counts as petrol). Only a Skoda
  petrol Kamiq, Scala, Octavia, Fabia or Karoq resolves, to `skoda_gas`. Superb and Rapid count as
  the model in any case right after Skoda (spaces or a hyphen between), and Superb also anywhere as
  written or in capitals: that escalates, or voids an identified car. The lowercase word superb
  elsewhere is not a model: it does not void an identified car, but a description that contains it
  never resolves on the text route (the reply asks for the plate number). A recognised make other than Skoda (the makes in `knowledge/fleet-catalog.json` and
  `_OTHER_MAKES` in `serve/app/tools.py`, in English, and the Hebrew spellings in `_HE_TO_MAKE`), or
  a known Skoda model or powertrain outside that list, escalates with no Skoda content. The words MG
  (also MG4), Seat, Genesis, Dodge, Jaguar, Jeep, Ford, Merc and Alfa count only as written
  or in capitals.
- Otherwise there is no assessment: the reply asks for the plate number, or says the caller number
  or plate number is not on record.
- The identified car is authoritative. The description voids it only by naming a recognised make
  other than the car's (the words above only as written or in capitals) or a known Skoda model other
  than the car's; powertrain words, unknown model names, plates, phone numbers and mileages in the
  text do not.
- On `/api/agent` an optional `customer_id` must agree with the caller number and the plate; any
  disagreement gives no assessment.
- A light that means stop gets "Do not drive" in every reply to a request whose description can be
  read, also when there is no assessment: carmind adds "Do not drive until the car has been
  checked." when, for any vehicle it covers, the description gets a do-not-drive reply. On
  `/api/agent` such a reply has the status `not_drivable`.

## Knowledge mounts

`CARMIND_KB_DIR` is the directory that contains `knowledge/`. In the container it is `/kb`
(knowledge at `/kb/knowledge`, schema at `/kb/schema`), mounted read-only. Tests run from the
repository set it to the repository root when it isn't already set.

At startup the indicator records and context rules are validated against
`schema/knowledge.schema.json`. A record that does not match (for example one without
`provenance.manual_id` or `provenance.source_page`) or a missing schema file stops the
service from starting; the error names the record and the failing field.

## Constrained choice (luna)

`LUNA_MODE=on` turns it on; the code default is `off`, and the compose file sets `on` for the app
unless `.env` says otherwise. It answers two questions, each from a closed list. The vehicle, which
lights the words match, the severity, the do-not-drive precaution, how context rules apply and the
dispatch rules stay in code.

- Context flags: asked only when the vehicle is identified, the description resolves to one drivable
  light or to several matched lights that are all drivable, the request carries no context flags
  other than `after_service_hours`, no context rule has already fired for those lights, and a rule
  could fire once an inferable flag is added. The allowed answers are any of `repeat_problem`,
  `recent_long_service`, `near_service_interval`, `fresh_handover` and `no_pre_delivery_test`, or
  none. The answer is added to the request's flags and goes through the same rules, so it can only
  make the verdict more cautious. The model gets keyword windows of +/-120 characters, at most 400,
  because words about a car's history often sit further from the light's name than the words that
  identify it. The fake provider follows a fixed rule on that snippet: `repeat_problem` when it
  contains again, came back, comes back, coming back, second time or third time (or שוב, פעם שנייה,
  פעם שלישית), nothing otherwise.
- Which light: asked only when the description matches two or more lights for the identified car,
  each is drivable on its own record, none is described in a state its record does not cover, and no
  context rule fires, inferred flags included. The allowed answers are the matched light ids plus
  `ask`; any of them is accepted, even when another matched light has a more cautious verdict. The
  reply is rendered from that light's record exactly as for a description that names one light, and
  names every matched light. The model gets keyword windows of +/-40 characters, at most 200. The
  fake provider picks the one light the description names with light, lamp, warning or indicator
  after it (or נורת or נורה before it), and answers `ask` otherwise.
- How it is constrained: the answers are sent as an enum, in an Anthropic tool (the request forces
  the tool unless the model refuses forced tool use, and then offers it; it uses strict tool use
  unless the model refuses that, and then sends the plain tool; the prompt always says to answer only
  by calling the tool, and an answer without it is no answer; each refusal is remembered for that
  model once the request without that setting works; while a model's first request with a setting is
  still pending, other requests for that model go without it; `CARMIND_ANTHROPIC_STRICT=off` always
  sends the plain tool), or
  in an OpenAI strict JSON schema. The schema asks for the reason before the ids. Code enforces the
  set. Each question can end without an answer: an answer outside the set, a reply that is not
  exactly a reason and the ids, a provider error, a missing key, the time budget
  (`LUNA_TOTAL_BUDGET_S`, default 10 s, at most 30, shared by both questions of a request) or 8
  calls already waiting on a real provider. A context question without an answer adds no flags, and
  the reply is what it would be without it. A light question without an answer leaves the assessment
  without a pick, which for several matched lights is the clarifying question. Flags from a context
  question that was answered stay in either case.
- Dispatch on `/api/agent`: with a pick, the dispatch is the more cautious of the plan for the
  picked light and the plan for an unconfirmed light: the higher action (tow to the dealer, then
  roadside rescue, then dealer appointment, then none), the covered plan on a tie, and never less
  coverage than the plan for an unconfirmed light.
- What the model sees: the masked snippets above (plate shapes, and every word that holds a digit or
  an @, in any form Unicode folds to one, replaced by `[REDACTED]`; a word cut by a window edge is
  replaced too when it holds one, or when it runs on for more than 254 characters), each light's
  name, meaning and matched words, and the listed facts. Other words near the matched words, a name
  for example, are sent as written. It never gets the customer record, the caller number or the
  plate field.
- What you see: on `POST /` the reply ends with paragraphs written by code that name the inferred
  flags and the picked light with the words that match it, or say that the model could not pick. The
  model's own reasons are never in that reply. On `/api/agent` the `context` and `choice` fields and
  the reply also carry the model's reasons, labelled "Model reasoning (not a verdict)", passed
  through the same word and character filter as the agent's note (it withholds listed verdict,
  drivability and dispatch wording; it does not judge meaning) and, when shown, cut to 300
  characters after Unicode normalisation; the operator page shows them in the "Context from the
  description" and "Light choice" cards.
- It uses `CARMIND_PROVIDER` and that provider's model id. With `fake` nothing leaves the machine;
  with a real provider the masked snippets, the light names and the listed facts do. No request or
  answer is written to disk or logged; a failure is logged by its type only.

The service writes no request content or context flags to disk (the tests check the service and
knowledge directories); the operator page's own saves stay in the browser (see "The operator page").
With a real provider and `LUNA_MODE=on`, the masked snippets, the light
names and the listed facts leave the machine.

## Operations

The compose file is the one at the repository root (`docker-compose.yml`, project `carmind`).
Run the wrapper from Git Bash or any POSIX shell:

```
serve/bin/serve-stack build        # build the image
serve/bin/serve-stack up           # app on 127.0.0.1:7600 and the mcp service
serve/bin/serve-stack test [args]  # pytest on serve/tests in a container (paths like tests/test_tools.py)
serve/bin/serve-stack tools-test   # pytest on tools/ in a python image with git and curl
serve/bin/serve-stack lint [--fix] # ruff on serve and tools
serve/bin/serve-stack probe        # list the MCP tools and call assess_warning_light once
serve/bin/serve-stack e2e          # HTTP e2e plus the Playwright browser e2e against the running stack
serve/bin/serve-stack stress       # 395 contract requests plus 160 agent requests, with safety assertions
serve/bin/serve-stack logs         # follow logs
serve/bin/serve-stack down         # stop and remove the project's containers
```

The browser e2e installs `playwright@1.63.0` from npm at run time, so it needs network.

`e2e` and `stress` send two preflight requests to `/api/agent` and exit 1 unless the app answers
with the `fake` provider and with luna on, so a real provider set in `.env` is never hit by their
many requests and the luna checks run against the compose default.

## Not implemented

`triage_checklist`, `render_form` and `drivability_gate` in `app/tools.py` are placeholders
that raise `NotImplementedError`.
