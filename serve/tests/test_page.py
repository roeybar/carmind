"""
The operator page: /api/config, the page headers, /app.js, the bundled fonts, the route list and a
static scan of index.html and app.js (no HTML sinks, no outside addresses, ASCII bytes).
"""
import hashlib
import re

from fastapi.routing import APIRoute

from app import agent, ui
from app.main import app
from app.models import AgentRequest

FAILED = ("The request failed. If the problem involves the brakes, the steering, smoke, fire or a red warning light, "
          "do not drive the car until it has been checked.")
EXAMPLES = [
    "Red brake warning light is on",
    "Check engine light came on",
    "ESC light is flashing",
    "Coolant warning came on after the engine control unit was updated",
    "Check engine light came on again",
]
BANNED = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "http://",
          "https://", "MediaRecorder", "FormData", "DOMParser", "createContextualFragment", "srcdoc", "writeln",
          "javascript:")
FETCH_PATHS = {"/api/config", "/api/customers", "/api/agent"}
API_ROUTES = {("/", "POST"), ("/assistant", "POST"), ("/chat", "POST"), ("/health", "GET"), ("/", "GET"),
              ("/api/customers", "GET"), ("/api/agent", "POST")}
PAGE_ROUTES = {("/api/config", "GET"), ("/app.js", "GET"), ("/fonts/{name}", "GET")}


def _static(name):
    return (ui.STATIC_DIR / name).read_bytes()


def test_config_keys_languages_and_theme(client):
    cfg = client.get("/api/config").json()
    assert set(cfg) == {"product_name", "theme", "languages", "reply_language", "capabilities", "limits", "examples",
                        "badges", "actions", "copy"}
    assert [lang["code"] for lang in cfg["languages"]] == ["en-US", "he-IL"]
    assert [lang["rtl"] for lang in cfg["languages"]] == [False, True]
    assert cfg["reply_language"] == "en-US"
    assert set(cfg["theme"]) == {"primary", "dark", "record"}
    assert all(re.fullmatch(r"#[0-9A-F]{6}", value) for value in cfg["theme"].values())


def test_every_string_is_ascii_and_not_empty(client):
    cfg = client.get("/api/config").json()
    strings = [cfg["product_name"], *cfg["copy"].values(), *cfg["badges"].values(), *cfg["actions"].values(),
               *cfg["examples"], *(lang["label"] for lang in cfg["languages"])]
    assert all(isinstance(s, str) and s and s.isascii() for s in strings)


def test_fixed_strings_are_exact(client):
    copy = client.get("/api/config").json()["copy"]
    assert copy["failed"] == FAILED
    assert copy["inputs_locked"] == ("This conversation's caller is fixed. Start a new conversation for another "
                                     "caller.")
    assert copy["privacy"] == (
        "Dictation and read-aloud run on this device; carmind never receives audio. Each question is sent to carmind "
        "to be answered and, when a real model provider (Anthropic or OpenAI) is configured, on to that provider. "
        "Saved conversations stay in this browser, encrypted with your passphrase, unless you save one to a file. "
        "All customers, places and companies are invented.")
    assert copy["dictation_server_only"] == (
        "Dictation here would send your voice to the browser's speech service, so it is off. Type, or use your phone "
        "keyboard's microphone.")
    # The header line and the empty-state text, verbatim.
    assert copy["status"] == "Call-center assistant demo - Aldente Cars Atlantis (fictional)"
    assert copy["empty_body"] == (
        "The verdict text comes from carmind's records. A language model writes the note, may infer listed "
        "car-history facts that can only make the verdict more cautious, and may pick which of several matched "
        "drivable lights the caller means; the dispatch is then the more cautious of the two plans. All customers, "
        "places and companies are invented.")
    assert client.get("/api/config").json()["examples"] == EXAMPLES


def test_limits_match_the_server():
    worst = agent.MAX_MODEL_TURNS * agent.MAX_CALLS_PER_TURN + 2 * len(agent.GUARDED_TOOLS) + 4
    assert ui.LIMITS["tool_calls"] >= worst
    caps = [m.max_length for m in AgentRequest.model_fields["problem"].metadata if hasattr(m, "max_length")]
    assert caps == [4000]
    assert ui.LIMITS["problem"] == caps[0]


def test_page_headers(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-security-policy"] == ui.PAGE_CSP
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-cache"


def test_app_js_is_served_as_javascript(client):
    resp = client.get("/app.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.content == _static("app.js")


def test_fonts_are_served_with_their_pinned_bytes(client):
    assert len(ui.FONT_FILES) == 14
    for name, digest in ui.FONT_FILES.items():
        resp = client.get(f"/fonts/{name}")
        assert resp.status_code == 200, name
        assert resp.headers["content-type"] == "font/woff2"
        assert hashlib.sha256(resp.content).hexdigest() == digest, name


def test_unknown_font_names_are_not_found(client):
    for path in ("/fonts/unknown.woff2", "/fonts/..%2Findex.html", "/fonts/app.js", "/fonts/LICENSE-assistant.txt"):
        assert client.get(path).status_code == 404, path


def test_route_list(client):
    routes = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert routes == API_ROUTES | PAGE_ROUTES


def test_static_files_have_no_sinks_and_only_known_fetches():
    for name in ("index.html", "app.js"):
        text = _static(name).decode("ascii")
        for banned in BANNED:
            assert banned not in text, (name, banned)
        calls = re.findall(r"fetch\(\s*([^,)]*)", text)
        assert all(arg.strip() in {f'"{p}"' for p in FETCH_PATHS} for arg in calls), (name, calls)
    assert "fetch(" in _static("app.js").decode("ascii")


def test_derived_keys_are_not_extractable():
    script = _static("app.js").decode("ascii")
    calls = re.findall(r"deriveKey\((.*?)\);", script, re.S)
    assert calls and all(re.search(r",\s*false,\s*\[\"encrypt\", \"decrypt\"\]\s*$", call) for call in calls), calls


def test_static_files_are_ascii():
    assert _static("index.html").isascii()
    assert _static("app.js").isascii()


def test_boot_failed_line_is_visible_by_default():
    page = _static("index.html").decode("ascii")
    tag = re.search(r"<[^>]*id=\"boot-failed\"[^>]*>", page)
    assert tag and "hidden" not in tag.group(0)
    assert ui.COPY["boot_failed"] in page


def test_repeated_config_and_font_requests_are_identical(client):
    first_config = client.get("/api/config").content
    first_font = client.get("/fonts/assistant-latin-400-normal.woff2").content
    for _ in range(200):
        assert client.get("/api/config").content == first_config
        assert client.get("/fonts/assistant-latin-400-normal.woff2").content == first_font
