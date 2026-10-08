# carmind

Warning-light lookup for roadside-assistance calls: a caller describes a dashboard light, and carmind returns a sourced verdict (meaning, what to tell the driver, drive or do not drive) with the manual and page it came from (warning-light knowledge is shared per make and powertrain).

![carmind answering a coolant warning light: a language model picks the light from a closed list, and the verdict comes from that light's record](docs/carmind-demo.png)

## Why it is interesting

- **The verdict is always a record's, never the model's words.** A model talks to carmind through MCP tools (customer, warning light, company procedure, dispatch). The verdict and the dispatch line in the reply are rendered by code from the tool results. The model's own text is shown only as a labelled note ("Model note (not a verdict)"), and it is withheld when it contains verdict, drivability, severity or dispatch wording from a fixed list in English, Hebrew or Arabic. A model may decide two things, each from a closed list: which listed car-history facts the description states, which can only make the verdict more cautious, and which of several matched drivable lights the caller means, which may be a less cautious one than another match. The verdict is then that light's own record, the reply names every matched light, and on the agent route the dispatch and its coverage are never less cautious than without the pick.
- **Constrained choice: the model picks from closed lists.** With luna on, carmind can ask a language model two questions, each answered from a fixed set sent as an enum in the provider's structured output (an Anthropic tool input schema, with strict tool use unless it is switched off, the model has refused it, or that model's first strict request is still pending; or an OpenAI strict JSON schema); the schema asks for a short reason before the ids, and code enforces the set. First, when a resolved light could be tightened by a context rule and the request carries no context flags other than after_service_hours, which of five car-history facts the description states (a repeated problem, a recent long garage stay, a service due, a fresh handover, no pre-delivery test): those flags are added to the request and go through the same rules, so they can only make the verdict more cautious. Second, when the description matches several lights that are all drivable on their own record, which one the caller means: any matched light, or `ask`, even one with a less cautious verdict than another matched light. The reply is rendered from the chosen light's record and names every matched light, and on the agent route the dispatch and its coverage are the more cautious of the plan for the picked light and the plan for an unconfirmed light. On the chat endpoint the reply names the inferred flags and the words behind a pick, written by code; the agent route and the operator page also show the model's reasons, labelled "Model reasoning (not a verdict)" and passed through the same word and character filter as the agent's note. A light that means stop, a light described in a state its record does not cover, a context rule that already fires, or an unidentified vehicle keeps the model out.
- **No vehicle, no guess.** carmind needs the vehicle and never reads a plate or a phone number from the description. The caller number is looked up first: a caller on record with one car identifies that car, and a caller with several cars is asked which one. The plate number field picks a car directly. On the chat endpoint, with no plate number and no caller on record (no caller number, or one that is not on record), the description can name a Skoda make, model and powertrain instead. Otherwise, or with a plate that is not on record or not one of the caller's cars, carmind gives no assessment: it asks for the plate number or escalates. A recognised make other than Skoda, a Skoda model or powertrain the manual data does not cover, a description that names another make or model than the identified car, and an unrecognised light get "contact a qualified specialist", never guidance meant for a different make or powertrain. A light that means stop gets "Do not drive" in every reply to a request whose description can be read, also when there is no assessment.
- **Runs with no key.** A fake model provider drives the demo and the tests, the constrained choices included: it follows fixed rules and makes no network call. Anthropic and OpenAI are bring-your-own-key.
- **The service never writes request content to disk.** The operator page can keep conversations in this browser, encrypted with a passphrase (AES-256-GCM, PBKDF2-SHA256, 600,000 iterations), and save one to a file.
- **Speech stays on the device.** Dictation runs only where the browser can recognise speech on the device, and its text goes into the problem box for you to send. Read-aloud uses a voice on the device. The page's language setting (English or Hebrew) chooses the dictation language and the layout direction; Hebrew is laid out right to left. Replies are in English, and read-aloud reads them with an English voice.
- **The page loads nothing from outside.** Its script, styles and fonts come from the service itself, and its Content-Security-Policy allows no other origin.

## Architecture

```
 browser UI  --POST /api/agent-->  app (FastAPI, :7600)
                                    |  agent loop: model provider
                                    |  (fake | anthropic | openai)
                                    |
                                    +--MCP (streamable HTTP)-->  mcp (:7601, internal)
                                                                  get_customer
                                                                  assess_warning_light
                                                                  get_company_procedure
                                                                  plan_dispatch
                                                                    |
                                    knowledge/ (JSON, read-only) <--+
                                    manual facts, customers, policy, dispatch model
```

The reply is assembled in this order: the model note (if it passes the guard), the inferred context and the light choice (when luna gave them), then `carmind verdict: ...` and `carmind dispatch: ...`, both built from tool fields. The app itself calls each tool the reply needs that has no good result yet (the model failed, skipped it or got an error), and repeats that call in-process if it fails; if the MCP service fails or the deadline passes, the tools still without a result run in-process.

All customers, plates, places and companies are invented (the fictional town of Fernhollow; the phone numbers are in the 555-0100 to 555-0199 range set aside for fiction).

## Quickstart

Requirements: Docker with Compose.

```
git clone <this repository> carmind
cd carmind
docker compose up -d --build
```

Open http://127.0.0.1:7600. The menu button at the start of the header opens the drawer. The first demo caller is already picked; you can pick another (it fills the caller number), and add a plate number if the caller has more than one car. Pick an example there, or type the warning light in the box at the bottom, then press the send button or Enter. Each answer shows carmind's reply with a verdict badge, and Details shows the dispatch, the model's labelled note and choices, the tool calls and the provider. Where the browser offers on-device speech recognition, you can also hold the microphone button and speak: the text goes into the box and is sent only when you send it.

Open it on the computer that runs it: dictation and browser saves need a secure page, and the compose file publishes the service on this computer only. The page is laid out for phone-sized screens too (try your browser's device emulation).

## Configuration

Optional. Copy `.env.example` to `.env` and set:

| Variable | Meaning |
|---|---|
| `CARMIND_PROVIDER` | `fake` (default), `anthropic` or `openai` |
| `ANTHROPIC_API_KEY`, `CARMIND_ANTHROPIC_MODEL` | key and model id when the provider is `anthropic` |
| `OPENAI_API_KEY`, `CARMIND_OPENAI_MODEL` | key and model id when the provider is `openai` |
| `LUNA_MODE` | `on` (the demo default) lets a language model infer listed car-history context flags (they can only tighten) and pick one of several matched drivable lights, from closed lists; `off` makes no model call |
| `LUNA_TOTAL_BUDGET_S` | seconds both luna calls of a request may take together (default 10, at most 30); after that there is no answer |

Keys are read from the environment only and never logged.

## Tests

All checks run in containers through one wrapper:

```
bash serve/bin/serve-stack test        # the service suite
bash serve/bin/serve-stack lint        # ruff
bash serve/bin/serve-stack e2e         # HTTP and browser end-to-end (Playwright)
bash serve/bin/serve-stack stress      # concurrent requests, fake provider only
bash serve/bin/serve-stack down
```

GitHub Actions runs the build, lint and test suites on every push.

## Status

Prototype, not deployed. It covers the Skoda petrol Kamiq, Scala, Octavia, Fabia and Karoq from one owner's manual, the Kamiq's: a comparison with the Octavia manual found the warning-light meanings shared across the make's petrol models (knowledge/manual-sharing-finding.md); any other recognised vehicle gets an escalation, and a request without an identified vehicle gets a request for the plate number. The customer data, company policy and dispatch model are synthetic.

## License

PolyForm-Noncommercial-1.0.0 (the PolyForm Noncommercial License 1.0.0). See [LICENSE](LICENSE).

The bundled Assistant, Noto Sans Arabic and IBM Plex Mono fonts are under the SIL Open Font License 1.1 (serve/app/static/fonts/).
