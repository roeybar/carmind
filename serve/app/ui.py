"""
The operator page's settings and assets: GET /api/config, GET /app.js and GET /fonts/{name}.

Every string the page shows, its colours, its languages and its limits come from /api/config;
index.html and app.js carry none of them, apart from the page title and the line shown when
the configuration cannot be loaded. Nothing here reads or writes request content.
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

STATIC_DIR = Path(__file__).resolve().parent / "static"
FONTS_DIR = STATIC_DIR / "fonts"

PRODUCT_NAME = "carmind"
THEME = {"primary": "#0F766E", "dark": "#1F2933", "record": "#DB2777"}
LANGUAGES = [{"code": "en-US", "label": "English (US)", "rtl": False},
             {"code": "he-IL", "label": "Hebrew (Israel)", "rtl": True}]
# /api/agent replies are in English whatever the dictation language is.
REPLY_LANGUAGE = "en-US"
LIMITS = {"problem": 4000, "caller": 40, "plate": 40, "import_bytes": 4194304, "messages": 400, "passphrase_min": 8,
          "tool_calls": 64, "assistant_text": 20000, "detail_text": 4000}
CAPABILITIES = {"dictation": "local_only", "read_aloud": "local_voices_only", "browser_saves": True, "file_import": True}

EXAMPLES = [
    "Red brake warning light is on",
    "Check engine light came on",
    "ESC light is flashing",
    "Coolant warning came on after the engine control unit was updated",
    "Check engine light came on again",
]
BADGES = {
    "not_drivable": "Not drivable",
    "drivable_caution": "Drivable with caution",
    "drivable": "Drivable",
    "unclear": "Unclear - needs clarification",
    "unsupported": "Unsupported vehicle",
}
ACTIONS = {
    "none": "No dispatch - talk the customer through it",
    "roadside_rescue": "Roadside rescue",
    "tow_to_dealer": "Tow to dealer",
    "dealer_appointment": "Dealer appointment",
}

COPY = {
    # Header and composer
    "status": "Call-center assistant demo - Aldente Cars Atlantis (fictional)",
    "empty_title": "Describe the warning light",
    "empty_body": ("The verdict text comes from carmind's records. A language model writes the note, may infer listed "
                   "car-history facts that can only make the verdict more cautious, and may pick which of several "
                   "matched drivable lights the caller means; the dispatch is then the more cautious of the two plans. "
                   "All customers, places and companies are invented."),
    "menu_label": "Open conversations",
    "new_label": "New",
    "mic_label": "Hold to speak, slide up to lock",
    "idle_hint": "Hold to speak, or tap once for hands-free",
    "recording_hint": "Slide up to lock - release to stop",
    "release_hint": "Release to review the transcript",
    "locked_hint": "Listening hands-free - tap to stop",
    "listening_lede": "LISTENING",
    "listening": "Listening...",
    "stop_label": "Stop dictation",
    "stop_review": "Stop and review",
    "read_aloud": "Read aloud",
    "read_stop": "Stop",
    "count_one": "1 message",
    "count_many": "{n} messages",
    "conversations_label": "CONVERSATIONS",
    "new_conversation": "+ New conversation",
    "ask_label": "Ask the agent",
    "asking": "Asking...",
    "problem_placeholder": "Describe the warning light the customer sees",
    "problem_first": "Type the customer's problem first.",
    "network_error": "Could not reach the service.",
    "request_failed": "Request failed.",
    "model_error_prefix": "Model: ",
    "customers_failed": "Could not load the customers.",
    "failed": ("The request failed. If the problem involves the brakes, the steering, smoke, fire or a red warning "
               "light, do not drive the car until it has been checked."),
    "details": "Details",
    "conversation_full": "This conversation is full. Start a new conversation.",
    "boot_failed": "The service is unavailable. Reload the page to try again.",
    # The drawer: the call, the examples, the language and the privacy line
    "call_label": "CUSTOMER CALL",
    "examples_label": "EXAMPLES",
    "language_label": "DICTATION LANGUAGE",
    "customer_label": "Demo caller",
    "caller_label": "Caller number",
    "caller_placeholder": "Caller ID of the incoming call",
    "plate_label": "Plate number",
    "plate_placeholder": "e.g. FH-104-27; leave empty if the caller has one car",
    "after_hours": "After hours",
    "inputs_locked": "This conversation's caller is fixed. Start a new conversation for another caller.",
    "privacy": ("Dictation and read-aloud run on this device; carmind never receives audio. Each question is sent to "
                "carmind to be answered and, when a real model provider (Anthropic or OpenAI) is configured, on to "
                "that provider. Saved conversations stay in this browser, encrypted with your passphrase, unless you "
                "save one to a file. All customers, places and companies are invented."),
    # The customer card
    "card_name": "Name",
    "card_phone": "Phone",
    "card_cars": "Cars",
    "card_coverage": "Coverage",
    "card_location": "Location",
    "not_on_record": "Caller not on record.",
    # The answer details
    "dispatch_title": "Dispatch",
    "note_title": "Model note (not a verdict)",
    "context_title": "Context from the description (not a verdict)",
    "choice_title": "Light choice (not a verdict)",
    "tools_title": "Tool calls (MCP)",
    "no_dispatch": "No dispatch plan.",
    "no_note": "No model note.",
    "provider_prefix": "Provider: ",
    "provider_none": "none",
    "source_prefix": "Source: ",
    "options_prefix": "Options: ",
    "reasoning_prefix": "Reasoning: ",
    "destination_prefix": "Destination: ",
    "covered_prefix": "Covered by ",
    "not_covered_prefix": "Not covered by ",
    "not_covered_suffix": " - customer pays",
    "by_prefix": " (by ",
    "by_sep": ") - ",
    # The luna lines
    "choice_picked": "Picked by a language model: ",
    "choice_picked_dispatch": ("The dispatch is the more cautious of the plan for this light and the plan for an "
                               "unconfirmed light."),
    "choice_asked": "The language model could not pick one light; ask the caller which light is on.",
    "choice_rejected": "The choice response failed validation, so carmind ignored it.",
    "choice_failed": "No usable choice was obtained, so carmind ignored it.",
    "choice_none": "No model choice is available.",
    "reasoning_withheld": "Reasoning: withheld by carmind's word and character filter.",
    "dispatch_card_note": "The more cautious of the plan for the picked light and the plan for an unconfirmed light.",
    "context_chosen": "Inferred by a language model: ",
    "context_tighten": "Context can only make the verdict more cautious.",
    "context_empty": "The language model found none of the listed facts in the description.",
    "context_rejected": "The context response failed validation, so carmind ignored it.",
    "context_failed": "No usable context answer was obtained, so carmind ignored it.",
    "context_none": "No model context inference was made.",
    # Saves, export and import
    "saves_label": "SAVES",
    "saves_set_intro": "Set a passphrase to keep these conversations in this browser.",
    "passphrase": "Passphrase",
    "passphrase_repeat": "Repeat the passphrase",
    "passphrase_warning": ("If you forget this passphrase, the saved conversations and saved files cannot be read. "
                           "carmind cannot recover it."),
    "set_pass": "Set passphrase",
    "passphrase_short": "Use at least 8 characters.",
    "passphrase_mismatch": "The two passphrases differ.",
    "unlock_intro": "Unlock the conversations saved in this browser.",
    "unlock": "Unlock",
    "unlock_wrong": "The passphrase does not open the saved conversations.",
    "force": "Start new saves with this passphrase",
    "force_note": "Saves it cannot open stay in this browser, locked.",
    "saved_state": "Saved in this browser, encrypted.",
    "locked_count": "Saved conversations under another passphrase: {n}",
    "damaged_count": "Saved records that cannot be read: {n}",
    "not_saved": "not saved",
    "saves_unavailable": "This browser cannot keep saves here. Save a conversation to a file to keep it.",
    "save_refused": "This browser refused the save. Save the conversation to a file instead.",
    "save_invalid": "carmind cannot save this conversation, in this browser or to a file.",
    "save_file": "Save to file",
    "delete_label": "Save to file and remove",
    "save_conflict": "Another tab changed this conversation, so it was saved as a new one.",
    "export_needs_passphrase": "Set a passphrase first; the file is encrypted with it.",
    "export_needs_unlock": "Unlock your saves first; the file is encrypted with your passphrase.",
    "export_invalid": "This conversation cannot be saved to a file.",
    "export_too_large": "This conversation is too large to save to a file.",
    "import": "Open a saved file",
    "import_too_large": "The file is too large to be a carmind conversation.",
    "import_not_carmind": "This is not a carmind conversation file.",
    "import_not_session": "This file does not open with your passphrase. If it was saved under another one, enter it.",
    "import_enter_passphrase": "Enter the passphrase this file was saved with.",
    "import_wrong": "The passphrase does not open this file, or the file is damaged.",
    "imported": "imported",
    "imported_notice": "Opened from a file. carmind did not check these answers again.",
    # Speech and the secure-page notice
    "dictation_server_only": ("Dictation here would send your voice to the browser's speech service, so it is off. "
                              "Type, or use your phone keyboard's microphone."),
    "dictation_none": "This browser has no dictation. Type, or use your phone keyboard's microphone.",
    "dictation_denied": "Microphone permission was declined, so dictation is off.",
    "dictation_stopped": "Dictation stopped. The text so far is in the box.",
    "dictation_installing": ("Getting this browser's on-device speech model for the language. "
                             "Try again in a moment."),
    "dictation_local_failed": ("This browser's on-device speech recognition did not start, so dictation is off. "
                               "Type instead."),
    "read_aloud_none": "This browser has no on-device voice for this reply, so it cannot read it aloud.",
    "insecure_context": ("Dictation and saves need a secure page: open carmind at http://127.0.0.1:7600 on the "
                         "computer that runs it."),
}

# style-src keeps 'unsafe-inline' for the page's own <style> block; scripts come only from /app.js.
PAGE_CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
            "connect-src 'self'; img-src 'self'; media-src 'none'; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types 'none'")
PAGE_HEADERS = {"Content-Security-Policy": PAGE_CSP, "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer", "Cache-Control": "no-cache"}

# The bundled fonts (npm @fontsource 5.3.0, SIL Open Font License 1.1), each pinned by its sha256.
FONT_FILES = {
    "assistant-latin-300-normal.woff2": "b34ea6bc693f3ab4217cbe70156c932e305369bdccf885e73080a9095f0dece8",
    "assistant-latin-400-normal.woff2": "15905fd40bfde9d39d14a7faa8e48fb0d86198f432be318205fc82db68216009",
    "assistant-latin-500-normal.woff2": "4f527c67a99502d63fc4ca3acfbf7815a615f94b6f8d3b2df82cee0c99d09a45",
    "assistant-latin-600-normal.woff2": "a332fddd3344052a1949c2a7a3515ba9b26db7a6a632662f4aaab43d354a486e",
    "assistant-latin-700-normal.woff2": "3fc207ba11ec63b6ee57c8fd6ad9561267e687b3df1680a4c644df769c5e1e37",
    "assistant-hebrew-300-normal.woff2": "05b13993ed12a6bb8874562f8e172d53c87dfb13d259f99026b0c140f321fcc5",
    "assistant-hebrew-400-normal.woff2": "aaed3fc950d2d71a3815160b3ae98d09088019df7306f4d935e7fc83df35c67e",
    "assistant-hebrew-500-normal.woff2": "6f00466a57ed54e5b72f1725b8423dea696b0e1eaca07b4f66538c1cdf3ea69a",
    "assistant-hebrew-600-normal.woff2": "1add33b7d28203b92411b810c783353fd1ce2611984295faa3bff9c4b29cfac6",
    "assistant-hebrew-700-normal.woff2": "7564600f98353de61989b92d4e01852201714fdd53b8521ff3a4d9e594bc10a4",
    "noto-sans-arabic-arabic-400-normal.woff2": "4e2ca0745c908761dc5c5db951662873887c59366fa1a5693ad22c0864abf1bd",
    "noto-sans-arabic-arabic-600-normal.woff2": "52a2cd4b0ff37ac84ef9af585af2eca0a300d49ea4934cf023827de54fdc354f",
    "ibm-plex-mono-latin-400-normal.woff2": "08949f728dc52d528e69b1667d15c89a5686a4ee9a296ff90983985f99c380f7",
    "ibm-plex-mono-latin-500-normal.woff2": "01d285447409c8a588692162439a038b8cbd7871309ee20267b0d2d91c6e8e22",
}

router = APIRouter()


@router.get("/api/config")
def config() -> dict:
    return {"product_name": PRODUCT_NAME, "theme": THEME, "languages": LANGUAGES, "reply_language": REPLY_LANGUAGE,
            "capabilities": CAPABILITIES, "limits": LIMITS, "examples": EXAMPLES, "badges": BADGES,
            "actions": ACTIONS, "copy": COPY}


@router.get("/app.js", include_in_schema=False)
def app_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript; charset=utf-8",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


@router.get("/fonts/{name}", include_in_schema=False)
def font(name: str) -> Response:
    if name not in FONT_FILES:
        return Response(status_code=404)
    return FileResponse(FONTS_DIR / name, media_type="font/woff2",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})
