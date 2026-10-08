// Browser end-to-end check of the operator page (Playwright, run by the ui-e2e compose service).
// The page is opened through a loopback proxy at http://127.0.0.1:7600, a secure context, so
// WebCrypto, IndexedDB and the speech APIs behave as they do for the operator.
import net from "node:net";
import { chromium } from "playwright";
import { runSaves, sealFile } from "./ui_e2e_saves.mjs";
import { runSpeech } from "./ui_e2e_speech.mjs";

export const SERVE_URL = process.env.SERVE_URL || "http://localhost:7600";
export const PAGE_URL = "http://127.0.0.1:7600";
export const FAILED = "The request failed. If the problem involves the brakes, the steering, smoke, fire or a red warning light, do not drive the car until it has been checked.";
// The service's last-resort reply for a description without a stop light: a 200, status unclear, no precaution in the text.
const LAST_RESORT_TEXT = "An error occurred while processing the request. Please contact a qualified specialist.";
export const LAST_RESORT = {
  error: "internal error", reply: LAST_RESORT_TEXT,
  verdict: { status: "unclear", severity: null, indicator: null, text: LAST_RESORT_TEXT, source: null },
  dispatch: null, note: "", tool_calls: [], provider: null, customer: null, choice: null, context: null,
};
export const DESKTOP = { viewport: { width: 1280, height: 860 } };
export const PHONE = { viewport: { width: 390, height: 844 }, deviceScaleFactor: 3, isMobile: true, hasTouch: true };
const CSP = "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; img-src 'self'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'; trusted-types 'none'";
let failed = false;
export let CFG = null;
export const pageErrors = [];
const offPage = [];

// Caller numbers and whether the server reads them as John Doe's (+1-555-0142).
const PHONE_ROWS = [
  ["+1-555-0142", true], ["1 (555) 0142", true], ["1.555.0142", true], [" +1-555-0142 ", true],
  [" +1-555-0142", true], ["+1-555-0142x", false], ["++1-555-0142", false], ["1-555-0142+", false],
  ["+1/555/0142", false], [String.fromCharCode(0xff11) + "-555-0142", false], ["+1-555-0142" + String.fromCharCode(0xfeff), false],
];

export function check(name, ok, detail = "") {
  if (ok) {
    console.log(`  PASS  ${name}`);
  } else {
    failed = true;
    console.log(`  FAIL  ${name}${detail ? ": " + detail : ""}`);
  }
}

// Every request of the context must go to the page's own origin (or be a blob: download).
export function watchRequests(context, base = PAGE_URL) {
  context.on("request", (req) => {
    const url = req.url();
    if (!url.startsWith(base) && !url.startsWith("blob:")) { offPage.push(url); }
  });
}

// The default stand-ins: on-device recognition (available for every language) and one on-device en-US voice.
export function fakeSpeech(o) {
  const log = { started: [], available: [], install: [], spoken: [], release: {} };
  window.__speech = log;
  const mode = o.sr || "local";
  const hide = (name) => Object.defineProperty(window, name, { value: undefined, configurable: true, writable: true });
  class FakeSR {
    constructor() {
      this.lang = ""; this.continuous = false; this.interimResults = false; this.onresult = null; this.onerror = null;
      if (o.construct === "throw") { throw new Error("construct"); }
    }
    start() {
      log.started.push({ lang: this.lang, processLocally: this.processLocally });
      if (o.start === "throw") { throw new Error("start"); }
      log.active = this;
      if (o.error) { setTimeout(() => this.onerror && this.onerror({ error: o.error }), 80); return; }
      const text = o.emit === undefined ? "red brake light is on" : o.emit;
      if (text) {
        setTimeout(() => this.onresult && this.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: text }], { isFinal: true })] }), 80);
      }
      if (o.end) { setTimeout(() => this.onend && this.onend(), 200); }
    }
    stop() { log.active = null; }
    abort() { log.active = null; }
  }
  if (mode === "local" || mode === "webkit-local") {
    FakeSR.prototype.processLocally = false;
    if (o.keepFalse) { Object.defineProperty(FakeSR.prototype, "processLocally", { get() { return false; }, set() {}, configurable: true }); }
    FakeSR.available = (opts) => {
      log.available.push(opts);
      const lang = opts && opts.langs && opts.langs[0];
      const how = (o.available && (o.available[lang] || o.available["*"])) || "available";
      if (how === "throw") { throw new Error("available"); }
      if (how === "reject") { return Promise.reject(new Error("available")); }
      if (how === "hang") { return new Promise(() => {}); }
      if (how === "hold") { return new Promise((resolve) => { log.release[lang] = resolve; }); }
      return Promise.resolve(how);
    };
    FakeSR.install = (opts) => {
      log.install.push(opts);
      if (o.install === "reject") { return Promise.reject(new Error("install")); }
      if (o.install === "hold") { return new Promise((resolve) => { log.releaseInstall = resolve; }); }
      return new Promise((resolve) => setTimeout(() => resolve(o.install === undefined ? true : o.install), 50));
    };
  }
  if (mode === "none") { hide("SpeechRecognition"); hide("webkitSpeechRecognition"); }
  else if (mode === "local") { window.SpeechRecognition = FakeSR; hide("webkitSpeechRecognition"); }
  else if (mode === "webkit-local" || mode === "webkit-server") { hide("SpeechRecognition"); window.webkitSpeechRecognition = FakeSR; }
  else { window.SpeechRecognition = FakeSR; window.webkitSpeechRecognition = FakeSR; }
  // A stand-in microphone: each getUserMedia answers only when the test releases it; tracks and audio contexts are counted.
  if (o.media === "hold") {
    const media = { asked: 0, release: [], tracks: [], contexts: [] };
    log.media = media;
    const getUserMedia = () => new Promise((resolve) => {
      const n = media.asked++;
      media.release.push(() => {
        const track = { n, stopped: false, stop() { this.stopped = true; } };
        media.tracks.push(track);
        resolve({ getTracks: () => [track] });
      });
    });
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
    class FakeAudioContext {
      constructor() { this.closed = false; media.contexts.push(this); }
      createAnalyser() { return { fftSize: 0, getByteTimeDomainData() {} }; }
      createMediaStreamSource() { return { connect() {} }; }
      close() { this.closed = true; return Promise.resolve(); }
    }
    Object.defineProperty(window, "AudioContext", { value: FakeAudioContext, configurable: true, writable: true });
    Object.defineProperty(window, "webkitAudioContext", { value: FakeAudioContext, configurable: true, writable: true });
  }
  const voices = (o.voices || [{ name: "Local English", lang: "en-US", localService: true }]);
  window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; this.lang = ""; this.voice = null; this.onend = null; } };
  const synth = {
    getVoices: () => voices,
    speak(u) { log.spoken.push({ text: u.text, lang: u.lang, voice: u.voice && u.voice.name, local: !!(u.voice && u.voice.localService) }); },
    cancel() {}, addEventListener() {}, removeEventListener() {},
  };
  Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
}

// A context with the stand-in speech engines, the request watch and the page-error log.
export async function newContext(browser, options = DESKTOP, speech = {}, base = PAGE_URL) {
  const context = await browser.newContext(options);
  await context.addInitScript(fakeSpeech, speech);
  watchRequests(context, base);
  context.on("page", (p) => p.on("pageerror", (err) => pageErrors.push(String(err))));
  return context;
}

export async function openPage(context, url = PAGE_URL) {
  const page = await context.newPage();
  page.on("dialog", (d) => d.accept().catch(() => {}));
  await page.goto(`${url}/`);
  await page.waitForFunction(() => document.getElementById("boot-failed").hidden === true, null, { timeout: 20000 });
  return page;
}

export async function openDrawer(page, touch = false) {
  if ((await page.getAttribute("#scrim", "data-open")) !== "1") {
    if (touch) { await page.tap("#menu"); } else { await page.click("#menu"); }
  }
}

export async function closeDrawer(page, touch = false) {
  if ((await page.getAttribute("#scrim", "data-open")) !== "1") { return; }
  const box = await page.locator("#veil").boundingBox();
  const rtl = (await page.getAttribute("#app", "dir")) === "rtl";
  const position = { x: rtl ? 20 : box.width - 20, y: Math.round(box.height / 2) };
  if (touch) await page.tap("#veil", { position }); else await page.click("#veil", { position });
}

export const last = (page) => page.locator("#thread .row.assistant").last();
export const lastText = async (page, sel = ".bubble") => (await last(page).locator(sel).allTextContents()).join("\n");

export async function waitAssistants(page, n) {
  await page.waitForFunction((k) => document.querySelectorAll("#thread .row.assistant").length >= k, n, { timeout: 20000 });
}

// New conversation, the drawer filled, then the problem asked; returns the /api/agent JSON (or null).
export async function ask(page, { customer, caller, plate = "", problem, status, fresh = true, after = false }) {
  if (fresh) { await page.click("#new"); }
  await openDrawer(page);
  if (customer) { await page.selectOption("#customer", customer); }
  if (caller !== undefined) { await page.fill("#caller", caller); }
  if (!(await page.isDisabled("#plate"))) { await page.fill("#plate", plate); }
  if (after) { await page.check("#after-hours"); }
  await closeDrawer(page);
  await page.fill("#problem", problem);
  const before = await page.locator("#thread .row.assistant").count();
  const answer = page.waitForResponse("**/api/agent", { timeout: 20000 }).catch(() => null);
  await page.click("#ask");
  await waitAssistants(page, before + 1);
  const resp = await Promise.race([answer, new Promise((resolve) => setTimeout(() => resolve(null), 1000))]);
  let json = null;
  try { json = resp ? await resp.json() : null; } catch (err) { json = null; }
  if (status !== undefined) {
    await page.waitForFunction((s) => {
      const rows = document.querySelectorAll("#thread .row.assistant");
      return rows.length && rows[rows.length - 1].getAttribute("data-status") === s;
    }, status, { timeout: 20000 }).catch(() => {});
  }
  return json;
}

// The bubble holds the server's verdict line and the row its status.
async function verdictShown(page, json) {
  if (!json || !json.verdict) { return false; }
  return (await lastText(page)).includes("carmind verdict: " + json.verdict.text) &&
    (await last(page).getAttribute("data-status")) === json.verdict.status;
}

async function waitForService(maxWaitMs = 60000) {
  const start = Date.now();
  while (Date.now() - start < maxWaitMs) {
    try {
      const resp = await fetch(`${SERVE_URL}/health`);
      if (resp.ok) {
        console.log(`[ui-e2e] Service ready at ${SERVE_URL}`);
        return;
      }
    } catch (err) {
      // not up yet
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`Service not ready after ${maxWaitMs / 1000}s`);
}

// 127.0.0.1:7600 -> the service, so the page is on a loopback (secure) origin.
function startProxy() {
  const target = new URL(SERVE_URL);
  const server = net.createServer((client) => {
    const upstream = net.connect(Number(target.port || 80), target.hostname);
    client.pipe(upstream);
    upstream.pipe(client);
    client.on("error", () => upstream.destroy());
    upstream.on("error", () => client.destroy());
  });
  return new Promise((resolve) => server.listen(7600, "127.0.0.1", () => resolve(server)));
}

// A page whose requests to the given paths wait until release[path]() is called.
async function heldPage(browser, paths) {
  const context = await newContext(browser);
  const page = await context.newPage();
  const release = {};
  for (const path of paths) {
    const held = new Promise((resolve) => { release[path] = resolve; });
    await page.route(`**${path}`, async (route) => { await held; await route.continue(); });
  }
  await page.goto(`${PAGE_URL}/`);
  await page.waitForFunction(() => document.getElementById("boot-failed").hidden === true, null, { timeout: 20000 });
  await openDrawer(page);
  return { page, release, context };
}

async function staticStrings(page) {
  const c = CFG.copy;
  const got = await page.evaluate(() => {
    const $ = (id) => document.getElementById(id);
    return {
      product: $("product").textContent, status: $("status").textContent, newLabel: $("new").textContent,
      newDrawer: $("newDrawer").textContent, menu: $("menu").getAttribute("aria-label"), mic: $("mic").getAttribute("aria-label"),
      ask: $("ask").getAttribute("aria-label"), core: $("core").getAttribute("aria-label"), stopwide: $("stopwide").textContent,
      problem: $("problem").getAttribute("placeholder"), caller: $("caller").getAttribute("placeholder"),
      plate: $("plate").getAttribute("placeholder"), pass: $("pass").getAttribute("placeholder"),
      pass2: $("pass2").getAttribute("placeholder"), privacy: $("privacy").textContent, hint: $("hint").textContent,
      locked: $("locked").textContent, warning: $("pass-warning").textContent, setPass: $("set-pass").textContent,
      unlock: $("unlock").textContent, force: $("force").textContent, importLabel: $("import").textContent,
      emptyTitle: document.querySelector(".empty h2").textContent, emptyBody: document.querySelector(".empty p").textContent,
      copyNodes: Array.from(document.querySelectorAll("[data-copy]")).map((n) => [n.getAttribute("data-copy"), n.textContent]),
      langs: Array.from(document.querySelectorAll("#lang option")).map((o) => o.textContent),
      examples: Array.from(document.querySelectorAll("button.example")).map((b) => b.textContent),
      title: document.title,
    };
  });
  const want = {
    product: CFG.product_name, status: c.status, newLabel: c.new_label, newDrawer: c.new_conversation, menu: c.menu_label,
    mic: c.mic_label, ask: c.ask_label, core: c.stop_label, stopwide: c.stop_review, problem: c.problem_placeholder,
    caller: c.caller_placeholder, plate: c.plate_placeholder, pass: c.passphrase, pass2: c.passphrase_repeat,
    privacy: c.privacy, hint: c.idle_hint, locked: c.inputs_locked, warning: c.passphrase_warning, setPass: c.set_pass,
    unlock: c.unlock, force: c.force, importLabel: c.import, emptyTitle: c.empty_title, emptyBody: c.empty_body,
  };
  const wrong = Object.keys(want).filter((k) => got[k] !== want[k]);
  got.copyNodes.forEach(([key, text]) => { if (c[key] !== text) { wrong.push(key); } });
  if (JSON.stringify(got.langs) !== JSON.stringify(CFG.languages.map((l) => l.label))) { wrong.push("languages"); }
  if (JSON.stringify(got.examples) !== JSON.stringify(CFG.examples)) { wrong.push("examples"); }
  if (got.title !== "carmind - call-center assistant demo") { wrong.push("title"); }
  return wrong;
}

async function core(browser) {
  const context = await newContext(browser);
  const page = await openPage(context);
  const c = CFG.copy;

  check("every visible string equals /api/config", (await staticStrings(page)).length === 0, (await staticStrings(page)).join(", "));
  const pageResp = await fetch(`${PAGE_URL}/`);
  check("the GET / CSP header is exact", pageResp.headers.get("content-security-policy") === CSP);

  await page.waitForSelector("#customer option", { state: "attached" });
  check("four demo callers in the select", (await page.locator("#customer option").count()) === 4);
  await page.waitForFunction(() => document.getElementById("caller").value !== "", null, { timeout: 20000 });
  check("picking a demo caller fills the caller number", (await page.inputValue("#caller")) === "+1-555-0142");
  await openDrawer(page);
  const plateBox = await page.locator("#plate").boundingBox();
  const callerBox = await page.locator("#caller").boundingBox();
  check("the drawer shows the plate field, labelled Plate number, below the caller number",
    (await page.innerText("label[for=plate]")) === "Plate number" && (await page.isVisible("#plate")) &&
      plateBox.y > callerBox.y);
  check("new inputs have maxlength 40",
    (await page.getAttribute("#caller", "maxlength")) === "40" && (await page.getAttribute("#plate", "maxlength")) === "40");
  check("the header names the fictional company", (await page.innerText("header")).includes("Aldente Cars Atlantis (fictional)"));

  await page.locator(".example").first().click();
  check("an example chip fills the problem", (await page.inputValue("#problem")).length > 0);

  let json = await ask(page, { customer: "C-1001", problem: "red brake warning light", status: "not_drivable" });
  check("verdict says Do not drive.", (await lastText(page)).includes("Do not drive.") && await verdictShown(page, json));
  check("the bubble has the model note label", (await lastText(page)).includes("Model note (not a verdict): "));
  check("the Details row is collapsed by default", (await last(page).locator("details.more").getAttribute("open")) === null);
  const toolCalls = await last(page).locator(".d-tools li").allTextContents();
  check("four tool calls", toolCalls.length === 4, String(toolCalls.length));
  check("assess_warning_light was called", toolCalls.some((t) => t.includes("assess_warning_light")));
  check("the company procedure is in the trace", toolCalls.some((t) => t.includes("get_company_procedure (by model) - not_drivable: ")));
  check("dispatch is a tow to the dealer", (await lastText(page, ".d-dispatch")).includes("Tow to dealer"));
  check("customer card shows the customer", (await page.textContent("#customer-card")).includes("John Doe"));

  await page.fill("#problem", "check engine light");
  check("typing in the draft after a result leaves the answer under its problem",
    (await last(page).getAttribute("data-status")) === "not_drivable" &&
      (await page.locator("#thread .row.user .bubble").last().textContent()) === "red brake warning light");
  await openDrawer(page);
  check("after a result the plate input is locked", await page.isDisabled("#plate"));
  check("after a result the demo caller input is locked", await page.isDisabled("#customer"));
  check("after a result the caller number input is locked", await page.isDisabled("#caller"));
  check("after a result the after-hours input is locked", await page.isDisabled("#after-hours") && await page.isVisible("#locked"));
  await page.locator(".example").nth(1).click();
  check("an example after a result fills the draft and leaves the answer",
    (await page.inputValue("#problem")) === CFG.examples[1] && (await last(page).getAttribute("data-status")) === "not_drivable");
  await page.click("#new");
  await openDrawer(page);
  check("New starts an empty thread with editable inputs",
    (await page.locator("#thread .row").count()) === 0 && (await page.isVisible(".empty")) &&
      !(await page.isDisabled("#plate")) && !(await page.isDisabled("#caller")) && !(await page.isDisabled("#customer")) &&
      !(await page.isDisabled("#after-hours")) && !(await page.isVisible("#locked")));
  await page.fill("#plate", "FH-104-27");
  await page.fill("#caller", "+1-555-0118");
  check("typing a caller number clears the plate", (await page.inputValue("#plate")) === "");
  await closeDrawer(page);

  json = await ask(page, { customer: "C-1004", problem: "red brake warning light", status: "not_drivable" });
  let verdictText = await lastText(page);
  check("a caller with two cars is asked which one",
    verdictText.includes("more than one car on record") && verdictText.includes("FH-452-63") &&
      verdictText.includes("FH-587-14") && verdictText.includes("Do not drive") && !verdictText.includes("FH-104-27") &&
      await verdictShown(page, json), verdictText);
  json = await ask(page, { customer: "C-1004", plate: "fh 452 63", problem: "red brake warning light", status: "not_drivable" });
  check("the plate number picks the car", (await lastText(page)).includes("Brake fluid level too low") && await verdictShown(page, json));
  json = await ask(page, { customer: "C-1004", plate: "FH-104-27", problem: "check engine light", status: "unsupported" });
  check("another customer's plate is unsupported",
    (await lastText(page)).includes("not one of this customer's cars") && await verdictShown(page, json));
  json = await ask(page, { caller: "+1-555-0199", problem: "check engine light", status: "unclear" });
  check("an unknown caller is unclear", (await lastText(page)).includes("caller number is not on record") && await verdictShown(page, json));
  check("the card says the caller is not on record", (await page.textContent("#customer-card")).includes("Caller not on record."));
  json = await ask(page, { caller: "+1-555-0199", plate: "FH-221-90", problem: "check engine light", status: "drivable_caution" });
  check("the plate number finds the customer", (await page.textContent("#customer-card")).includes("Jane Doe") && await verdictShown(page, json));
  json = await ask(page, { customer: "C-1001", problem: "FH-318-05 ESC light is flashing", status: "drivable" });
  check("a plate in the problem text is not read", (await last(page).getAttribute("data-status")) === "drivable" && await verdictShown(page, json));
  for (const [value, known] of PHONE_ROWS) {
    await page.click("#new");
    await openDrawer(page);
    await page.fill("#caller", value);
    const card = await page.textContent("#customer-card");
    check(`the caller card follows the server's reading of ${JSON.stringify(value)}`, card.includes("John Doe") === known, card);
    await closeDrawer(page);
    await page.fill("#problem", "check engine light");
    await page.click("#ask");
    await waitAssistants(page, 1);
    const after = await page.textContent("#customer-card");
    check(`the server reads ${JSON.stringify(value)} as the card did`, after.includes("John Doe") === known, after);
  }

  // A held answer stays in its own conversation.
  await page.route("**/api/agent", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.continue();
  });
  await page.click("#new");
  await page.fill("#problem", "ESC light is flashing");
  await page.click("#ask");
  const aId = await page.getAttribute('.chat[data-active="1"]', "data-id");
  await openDrawer(page);
  check("the caller inputs are locked while a request is pending",
    await page.isDisabled("#customer") && await page.isDisabled("#caller") && await page.isDisabled("#plate") &&
      await page.isDisabled("#after-hours"));
  await page.evaluate(() => { document.getElementById("plate").value = "FH-221-90"; });
  await closeDrawer(page);
  await page.click("#new");
  check("the ask button of another conversation is ready while one is pending",
    !(await page.isDisabled("#ask")) && (await page.getAttribute("#ask", "aria-label")) === c.ask_label);
  await page.waitForTimeout(3000);
  check("a conversation switch while the request is pending keeps the answer out of the open thread",
    (await page.locator("#thread .row").count()) === 0);
  await openDrawer(page);
  await page.click(`.chat[data-id="${aId}"]`);
  check("the late answer is in its own conversation", (await last(page).getAttribute("data-status")) === "drivable");
  check("an answer repaints the caller and plate it was asked for",
    (await page.inputValue("#plate")) === "" && (await page.inputValue("#caller")) === "+1-555-0142");
  check("the ask button is ready after the late answer",
    !(await page.isDisabled("#ask")) && (await page.getAttribute("#ask", "aria-label")) === c.ask_label);

  // Enter while a request is pending sends nothing more.
  let agentRequests = 0;
  const countAgent = (req) => { if (req.url().endsWith("/api/agent")) { agentRequests++; } };
  page.on("request", countAgent);
  await page.click("#new");
  await page.fill("#problem", "check engine light");
  await page.press("#problem", "Enter");
  await page.fill("#problem", "check engine light");
  await page.press("#problem", "Enter");
  await page.press("#problem", "Enter");
  await waitAssistants(page, 1);
  await page.waitForTimeout(500);
  check("Enter sends", (await page.locator("#thread .row.assistant").count()) === 1);
  check("Enter while a request is pending sends no second /api/agent request", agentRequests === 1, String(agentRequests));
  page.off("request", countAgent);

  // An answer for A while B is open leaves B's drawer, card and notice alone.
  await ask(page, { customer: "C-1001", problem: "red brake warning light", status: "not_drivable" }).catch(() => null);
  await page.click("#new");
  await page.fill("#problem", "red brake warning light");
  await page.click("#ask");
  const heldId = await page.getAttribute('.chat[data-active="1"]', "data-id");
  await page.click("#new");
  await openDrawer(page);
  await page.selectOption("#customer", "C-1002");
  const bBefore = [await page.inputValue("#caller"), await page.inputValue("#plate"), await page.textContent("#customer-card")];
  await page.waitForTimeout(3000);
  const bAfter = [await page.inputValue("#caller"), await page.inputValue("#plate"), await page.textContent("#customer-card")];
  check("an answer for one conversation arriving while another is open leaves the open drawer and card unchanged",
    JSON.stringify(bBefore) === JSON.stringify(bAfter) && (await page.textContent("#error")) === "" && bAfter[2].includes("Jane Doe"));
  await page.click(`.chat[data-id="${heldId}"]`);
  check("switching back shows the held answer and its card",
    (await last(page).getAttribute("data-status")) === "not_drivable" && (await page.textContent("#customer-card")).includes("John Doe"));
  await page.unroute("**/api/agent");

  json = await ask(page, { customer: "C-1003", problem: "ESC light is flashing", status: "unsupported" });
  check("unsupported car gets no vehicle information",
    (await lastText(page)).includes("No information found for the specified vehicle") && await verdictShown(page, json));
  await ask(page, { customer: "C-1002", problem: "check engine light", status: "drivable_caution" });
  check("drivable result shows a Drivable badge", (await lastText(page, ".badge")).includes("Drivable"));

  await page.route("**/api/agent", (route) => route.abort());
  await page.click("#new");
  await page.fill("#problem", "check engine light");
  await page.click("#ask");
  await page.waitForFunction(
    () => document.getElementById("error").textContent === "Could not reach the service.", null, { timeout: 20000 });
  check("network failure shows the fixed precaution", (await lastText(page)).includes(FAILED));
  check("network failure: the latest answer has no verdict status or badge",
    (await last(page).getAttribute("data-status")) === "" && (await last(page).locator(".badge").count()) === 0);
  check("network failure: the latest answer has no dispatch", (await last(page).locator(".d-dispatch").count()) === 0);
  check("network failure: the latest answer has no tool calls", (await last(page).locator(".d-tools li").count()) === 0);
  const failedId = await page.getAttribute('.chat[data-active="1"]', "data-id");
  await page.unroute("**/api/agent");
  await page.click("#new");
  check("switching conversations switches the notice (to empty)", (await page.textContent("#error")) === "");
  await openDrawer(page);
  await page.click(`.chat[data-id="${failedId}"]`);
  check("switching conversations switches the card and the notice",
    (await page.textContent("#error")) === "Could not reach the service.");

  await page.route("**/api/agent", (route) => route.fulfill({
    status: 500, contentType: "application/json", body: JSON.stringify({ error: "Test failure." }),
  }));
  await page.click("#new");
  await page.fill("#problem", "check engine light");
  await page.click("#ask");
  await page.waitForFunction(
    () => document.getElementById("error").textContent === "Test failure.", null, { timeout: 20000 });
  check("http failure shows the fixed precaution", (await lastText(page)).includes(FAILED));
  check("http failure: the latest answer has no verdict status or badge",
    (await last(page).getAttribute("data-status")) === "" && (await last(page).locator(".badge").count()) === 0);
  check("http failure: the latest answer has no dispatch", (await last(page).locator(".d-dispatch").count()) === 0);
  check("http failure: the latest answer has no tool calls", (await last(page).locator(".d-tools li").count()) === 0);
  await page.unroute("**/api/agent");

  await page.route("**/api/agent", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.abort();
  });
  await page.click("#new");
  await page.fill("#problem", "check engine light");
  await page.click("#ask");
  const lateFailId = await page.getAttribute('.chat[data-active="1"]', "data-id");
  await page.click("#new");
  await page.waitForTimeout(3000);
  check("a failure for a conversation that is not open shows nothing in the open thread and an empty #error",
    (await page.locator("#thread .row").count()) === 0 && (await page.textContent("#error")) === "");
  await openDrawer(page);
  await page.click(`.chat[data-id="${lateFailId}"]`);
  check("it is recorded as FAILED in its own conversation", (await lastText(page)).includes(FAILED));
  await page.unroute("**/api/agent");

  await page.route("**/api/agent", (route) => route.fulfill({
    status: 500, contentType: "text/html", body: "<html>Internal Server Error</html>",
  }));
  await page.click("#new");
  await page.fill("#problem", "check engine light");
  await page.click("#ask");
  await page.waitForFunction(
    () => document.getElementById("error").textContent === "Request failed.", null, { timeout: 20000 });
  check("a non-JSON 500 shows the fixed precaution", (await lastText(page)).includes(FAILED));
  await page.unroute("**/api/agent");

  const REJECTED = "invalid request Do not drive until the car has been checked.";
  await page.route("**/api/agent", (route) => route.fulfill({
    status: 400, contentType: "application/json",
    body: JSON.stringify({ error: "invalid request", precaution: "Do not drive until the car has been checked." }),
  }));
  await page.click("#new");
  await page.fill("#problem", "red brake warning light");
  await page.click("#ask");
  await waitAssistants(page, 1);
  check("a 400 gives a bubble and the #error string",
    (await lastText(page, ".reply")) === REJECTED && (await page.textContent("#error")) === REJECTED);
  await page.unroute("**/api/agent");

  // A 200 whose nested fields have the wrong type is a FAILED answer, and Ask stays usable.
  const misshapen = [
    ["dispatch.steps as a string", (b) => { b.dispatch = { ...b.dispatch, steps: "tow it" }; }],
    ["choice.options as a string", (b) => { b.choice = { status: "picked", picked: "x", options: "abc", reasoning: "", withheld: false }; }],
    ["tool_calls as an object", (b) => { b.tool_calls = { name: "assess_warning_light" }; }],
  ];
  for (const [label, edit] of misshapen) {
    await page.route("**/api/agent", async (route) => {
      const resp = await route.fetch();
      const body = await resp.json();
      edit(body);
      await route.fulfill({ response: resp, json: body });
    });
    await page.click("#new");
    await page.fill("#problem", "red brake warning light");
    await page.click("#ask");
    await waitAssistants(page, 1).catch(() => {});
    await page.waitForTimeout(300);
    check(`a 200 with ${label} is shown as FAILED and Ask stays enabled`,
      (await page.locator("#thread .row.assistant").count()) === 1 && (await lastText(page)) === FAILED &&
        (await page.textContent("#error")) === c.request_failed && !(await page.isDisabled("#ask")));
    await page.unroute("**/api/agent");
  }

  // A 200 without a string reply, or with a status outside the five, is a FAILED answer.
  for (const [label, body] of [["no reply", { verdict: { status: "not_drivable", text: "x" } }],
                               ["an unknown status", { reply: "carmind verdict: x", verdict: { status: "safe", text: "x" } }]]) {
    await page.route("**/api/agent", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) }));
    await page.click("#new");
    await page.fill("#problem", "red brake warning light");
    await page.click("#ask");
    await waitAssistants(page, 1);
    check(`a malformed 200 (${label}) is shown as FAILED`,
      (await lastText(page)) === FAILED && (await last(page).getAttribute("data-status")) === "" &&
        (await last(page).locator("details.more").count()) === 0 && (await page.textContent("#error")) === c.request_failed);
    await page.unroute("**/api/agent");
  }

  // The service's last-resort 200 ("internal error") is a FAILED answer; any other error on a 200 stays an answer.
  await page.route("**/api/agent", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LAST_RESORT) }));
  await page.click("#new");
  await page.fill("#problem", "smoke from engine");
  await page.click("#ask");
  await waitAssistants(page, 1);
  check("the last-resort 200 (internal error) for smoke from engine is shown as FAILED",
    (await lastText(page)) === FAILED && (await last(page).getAttribute("data-status")) === "" &&
      (await last(page).locator("details.more").count()) === 0 && (await page.textContent("#error")) === c.request_failed);
  await page.unroute("**/api/agent");
  let modelError = null;
  await page.route("**/api/agent", async (route) => {
    const resp = await route.fetch();
    modelError = await resp.json();
    modelError.error = "Test model error.";
    await route.fulfill({ response: resp, json: modelError });
  });
  await page.click("#new");
  await page.fill("#problem", "red brake warning light");
  await page.click("#ask");
  await waitAssistants(page, 1);
  check("a 200 with another error stays an answer with the model notice",
    (await lastText(page)) !== FAILED && (await last(page).getAttribute("data-status")) === modelError.verdict.status &&
      (await page.textContent("#error")) === c.model_error_prefix + "Test model error.");
  await page.unroute("**/api/agent");

  await page.click("#new");
  check("the empty state and the drawer say the data is invented",
    (await page.textContent(".empty p")).includes("All customers, places and companies are invented.") &&
      (await page.textContent("#privacy")).includes("All customers, places and companies are invented."));
  await page.fill("#problem", "x".repeat(4001));
  check("#problem has maxlength 4000", (await page.inputValue("#problem")).length === 4000);
  await page.fill("#problem", "");

  // luna with the fake provider (the compose default)
  const CHIP = "Coolant warning came on after the engine control unit was updated";
  const CHIP2 = "Check engine light came on again";
  const FAKE_PICK_REASON = "Fixed rule of the fake provider: only this light is named with a word for a warning light next to it.";
  const FAKE_FLAGS_REASON = "Fixed rule of the fake provider: the description says the problem happened again.";
  const UI_TEXT = {
    picked_dispatch: "The dispatch is the more cautious of the plan for this light and the plan for an unconfirmed light.",
    rejected: "The choice response failed validation, so carmind ignored it.",
    failed: "No usable choice was obtained, so carmind ignored it.",
    none: "No model choice is available.",
    reasoning_withheld: "Reasoning: withheld by carmind's word and character filter.",
    dispatch_card: "The more cautious of the plan for the picked light and the plan for an unconfirmed light.",
    context_empty: "The language model found none of the listed facts in the description.",
    context_rejected: "The context response failed validation, so carmind ignored it.",
    context_failed: "No usable context answer was obtained, so carmind ignored it.",
    context_none: "No model context inference was made.",
  };
  const chips = page.locator(".example");
  check("five example chips", (await chips.count()) === 5, String(await chips.count()));
  check("the fourth chip is the coolant example", (await chips.nth(3).textContent()) === CHIP);
  check("the fifth chip is the repeat example", (await chips.nth(4).textContent()) === CHIP2);

  async function askChip(customer, n, status) {
    await page.click("#new");
    await openDrawer(page);
    await page.selectOption("#customer", customer);
    await page.fill("#plate", "");
    await chips.nth(n).click();
    await page.click("#ask");
    await waitAssistants(page, 1);
    await page.waitForSelector(`#thread .row.assistant[data-status="${status}"]`, { timeout: 20000 }).catch(() => {});
  }

  await askChip("C-1002", 3, "drivable_caution");
  const choiceText = await lastText(page, ".d-choice");
  check("the choice card names the picked light", choiceText.includes("Picked by a language model: Coolant level too low"), choiceText);
  check("the choice card shows the fake provider's reason", choiceText.includes(FAKE_PICK_REASON), choiceText);
  check("the choice card says how the dispatch is chosen", choiceText.includes(UI_TEXT.picked_dispatch), choiceText);
  const dispatchText = await lastText(page, ".d-dispatch");
  check("the dispatch card shows roadside rescue and the more cautious plan",
    dispatchText.includes("Roadside rescue") && dispatchText.includes(UI_TEXT.dispatch_card), dispatchText);
  check("the context card says no listed fact was found", (await lastText(page, ".d-context")).includes(UI_TEXT.context_empty));
  await page.fill("#problem", "check engine light");
  check("a draft edit after a luna answer leaves its Details choice and context intact",
    (await lastText(page, ".d-choice")).includes("Picked by a language model: Coolant level too low") &&
      (await lastText(page, ".d-context")).includes(UI_TEXT.context_empty));
  await page.fill("#problem", "");

  await askChip("C-1002", 4, "not_drivable");
  const contextText = await lastText(page, ".d-context");
  check("the context card shows the inferred repeat and its reason",
    contextText.includes("repeat_problem") && contextText.includes(FAKE_FLAGS_REASON), contextText);
  check("the inferred repeat is a tow", (await lastText(page, ".d-dispatch")).includes("Tow to dealer"));

  await ask(page, { customer: "C-1001", problem: "red brake warning light", status: "not_drivable" });
  check("a stop light shows no model choice", (await lastText(page, ".d-choice")).includes(UI_TEXT.none));
  check("a stop light shows no context inference", (await lastText(page, ".d-context")).includes(UI_TEXT.context_none));

  const crafted = [
    ["a rejected choice", (b) => { b.choice.status = "rejected"; }, ".d-choice", UI_TEXT.rejected],
    ["a failed choice", (b) => { b.choice.status = "failed"; }, ".d-choice", UI_TEXT.failed],
    ["withheld reasoning", (b) => { b.choice.withheld = true; b.choice.reasoning = ""; }, ".d-choice", UI_TEXT.reasoning_withheld],
    ["no choice after a failed turn", (b) => { b.choice = null; b.error = "the agent turn failed."; }, ".d-choice", UI_TEXT.none],
    ["a rejected context", (b) => { b.context = { ...b.context, status: "rejected" }; }, ".d-context", UI_TEXT.context_rejected],
    ["a failed context", (b) => { b.context = { ...b.context, status: "failed" }; }, ".d-context", UI_TEXT.context_failed],
    ["no listed facts", (b) => { b.context = { ...b.context, status: "none", flags: [] }; }, ".d-context", UI_TEXT.context_empty],
  ];
  for (const [label, edit, card, expected] of crafted) {
    await page.route("**/api/agent", async (route) => {
      const resp = await route.fetch();
      const body = await resp.json();
      edit(body);
      await route.fulfill({ response: resp, json: body });
    });
    await askChip("C-1002", 3, "drivable_caution");
    const text = await lastText(page, card);
    check(`the page shows ${label}`, text.includes(expected), text);
    await page.unroute("**/api/agent");
  }

  await askChip("C-1002", 3, "drivable_caution");
  await page.route("**/api/agent", (route) => route.abort());
  await page.fill("#problem", "check engine light");
  await page.click("#ask");
  await page.waitForFunction(
    () => document.getElementById("error").textContent === "Could not reach the service.", null, { timeout: 20000 });
  check("network failure: the latest answer has no choice or context",
    (await last(page).locator(".d-choice").count()) === 0 && (await last(page).locator(".d-context").count()) === 0);
  await page.unroute("**/api/agent");

  // RTL
  await openDrawer(page);
  await page.selectOption("#lang", "he-IL");
  const rtl = await page.getAttribute("#app", "dir");
  const autoDirs = await page.evaluate(() => Array.from(document.querySelectorAll("#thread .row.user .bubble, #thread .bubble .reply"))
    .every((n) => n.getAttribute("dir") === "auto"));
  await page.selectOption("#lang", "en-US");
  check("he-IL lays the page out right to left with bubbles in dir=auto; en-US left to right",
    rtl === "rtl" && autoDirs && (await page.getAttribute("#app", "dir")) === "ltr");
  await closeDrawer(page);

  // The customer list arriving late or early.
  const late = await heldPage(browser, ["/api/customers", "/api/agent"]);
  await late.page.fill("#caller", "+1-555-0118");
  await late.page.fill("#plate", "FH-221-90");
  await closeDrawer(late.page);
  await late.page.fill("#problem", "ESC light is flashing");
  await late.page.click("#ask");
  late.release["/api/customers"]();
  await late.page.waitForSelector("#customer option", { state: "attached" });
  check("a late customer list keeps the typed caller number", (await late.page.inputValue("#caller")) === "+1-555-0118");
  check("a late customer list keeps the plate", (await late.page.inputValue("#plate")) === "FH-221-90");
  check("a late customer list picks no demo caller", (await late.page.inputValue("#customer")) === "");
  late.release["/api/agent"]();
  await late.page.waitForSelector('#thread .row.assistant[data-status="drivable"]', { timeout: 20000 });
  check("the answer is shown with the caller and plate it was asked for",
    (await late.page.inputValue("#caller")) === "+1-555-0118" && (await late.page.inputValue("#plate")) === "FH-221-90" &&
      (await late.page.textContent("#customer-card")).includes("Jane Doe") &&
      (await lastText(late.page)).includes("ESC"));
  await late.context.close();

  const early = await heldPage(browser, ["/api/customers", "/api/agent"]);
  await early.page.fill("#caller", "+1-555-0118");
  await early.page.fill("#plate", "FH-221-90");
  await closeDrawer(early.page);
  await early.page.fill("#problem", "ESC light is flashing");
  await early.page.click("#ask");
  early.release["/api/agent"]();
  await early.page.waitForSelector('#thread .row.assistant[data-status="drivable"]', { timeout: 20000 });
  check("an answer before the customer list never says the caller is not on record",
    !(await early.page.textContent("#customer-card")).includes("Caller not on record."));
  early.release["/api/customers"]();
  await early.page.waitForSelector("#customer option", { state: "attached" });
  check("the customer list arriving after the answer shows the caller",
    (await early.page.textContent("#customer-card")).includes("Jane Doe") && (await lastText(early.page)).includes("ESC"));
  await early.context.close();

  const typed = await heldPage(browser, ["/api/customers"]);
  await typed.page.fill("#caller", "+1-555-0177");
  typed.release["/api/customers"]();
  await typed.page.waitForSelector("#customer option", { state: "attached" });
  check("a caller number typed before the list arrives is kept",
    (await typed.page.inputValue("#caller")) === "+1-555-0177" && (await typed.page.inputValue("#customer")) === "");
  await typed.context.close();

  const plated = await heldPage(browser, ["/api/customers"]);
  await plated.page.fill("#plate", "FH-221-90");
  plated.release["/api/customers"]();
  await plated.page.waitForSelector("#customer option", { state: "attached" });
  check("a typed plate keeps the list from filling the caller",
    (await plated.page.inputValue("#caller")) === "" && (await plated.page.inputValue("#customer")) === "");
  await plated.context.close();

  // A conversation at the message cap (399 messages, imported) refuses to send.
  const PASS = "cap-check-passphrase";
  await openDrawer(page);
  await page.fill("#pass", PASS);
  await page.fill("#pass2", PASS);
  await page.click("#set-pass");
  await page.waitForFunction(() => document.getElementById("pass").hidden, null, { timeout: 20000 });
  const capFile = await sealFile(PASS, { messages: 399 });
  await page.setInputFiles("#import-file", { name: "cap.json", mimeType: "application/json", buffer: Buffer.from(capFile) });
  await page.waitForFunction(() => document.querySelectorAll("#thread .row").length === 399, null, { timeout: 30000 });
  let capRequests = 0;
  const countCap = (req) => { if (req.url().endsWith("/api/agent")) { capRequests++; } };
  page.on("request", countCap);
  await page.fill("#problem", "check engine light");
  await page.click("#ask");
  await page.waitForTimeout(500);
  page.off("request", countCap);
  check("a conversation at the message cap refuses to send",
    capRequests === 0 && (await page.textContent("#error")) === c.conversation_full);

  const boot = await newContext(browser);
  const bootPage = await boot.newPage();
  await bootPage.route("**/api/config", (route) => route.fulfill({ status: 500, body: "no" }));
  await bootPage.goto(`${PAGE_URL}/`);
  await bootPage.waitForTimeout(1000);
  check("the boot-failed line shows when /api/config fails",
    (await bootPage.isVisible("#boot-failed")) && (await bootPage.isDisabled("#ask")));
  await boot.close();

  await openDrawer(page);
  const text = await page.evaluate(() => document.body.innerText);
  check("page text is ASCII", /^[\x00-\x7F]*$/.test(text));
  await context.close();
}

// The phone layout at an emulated 390x844 phone.
async function phone(browser) {
  const context = await newContext(browser, PHONE);
  const page = await openPage(context);
  const errorsBefore = pageErrors.length;
  const c = CFG.copy;
  const noHScroll = () => page.evaluate(() => document.scrollingElement.scrollWidth <= window.innerWidth);
  const box = (sel) => page.locator(sel).boundingBox();
  await page.waitForSelector("#customer option", { state: "attached" });

  const header = await box("header"), footer = await box("footer"), thread = await box("#thread"), app = await box("#app");
  check("phone: the header, thread and composer fill the screen",
    header.y === 0 && Math.abs(footer.y + footer.height - 844) <= 1 && thread.y >= header.y + header.height - 1 &&
      thread.y + thread.height <= footer.y + 1 && Math.round(app.height) === 844);
  check("phone: no secure-context notice on localhost",
    (await page.evaluate(() => window.isSecureContext)) === true &&
      !(await page.textContent("#error")).includes(c.insecure_context) &&
      !(await page.textContent("#saves-state")).includes(c.insecure_context));
  check("phone: no horizontal scroll (empty)", await noHScroll());

  await page.tap("#menu");
  const aside = await box("aside");
  const maxWidth = await page.evaluate(() => getComputedStyle(document.querySelector("aside")).maxWidth);
  let inside = true;
  for (const sel of ["#customer", "#caller", "#plate", "#after-hours"]) {
    await page.locator(sel).scrollIntoViewIfNeeded();
    inside = inside && await page.isVisible(sel);
  }
  const drawerOk = (await page.getAttribute("#scrim", "data-open")) === "1" && aside.width <= 312 && maxWidth === "310px" &&
    Math.round(aside.x) === 0 && inside && await noHScroll();
  await closeDrawer(page, true);
  check("phone: the drawer opens and closes", drawerOk && (await page.getAttribute("#scrim", "data-open")) === "0");

  const within = async (sel) => { const b = await box(sel); return b && b.x >= 0 && b.y >= 0 && b.x + b.width <= 390 && b.y + b.height <= 844; };
  const askBox = await box("#ask"), micBox = await box("#mic");
  check("phone: the composer",
    await page.isVisible("#problem") && await page.isVisible("#ask") && await page.isVisible("#mic") &&
      await within("#problem") && await within("#ask") && await within("#mic") && askBox.width >= 44 && askBox.height >= 44 &&
      micBox.width >= 74 && micBox.height >= 74 && (await page.getAttribute("#problem", "maxlength")) === "4000");
  await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 10000 }).catch(() => {});
  check("phone: the mic is shown and enabled when on-device dictation is available",
    await page.isVisible("#mic") && !(await page.isDisabled("#mic")) && (await page.textContent("#hint")) === c.idle_hint);

  let agentCalls = 0;
  page.on("request", (req) => { if (req.url().endsWith("/api/agent")) { agentCalls++; } });
  await page.tap("#mic", { force: true });  // the idle mic bounces, so it is never "stable"
  await page.waitForFunction(() => document.getElementById("heardBox").textContent.includes("red brake"), null, { timeout: 5000 }).catch(() => {});
  // Locked hands-free shows the square on the mic (the Stop and review button is always shown, as specified).
  const overlayOpen = (await page.getAttribute("#overlay", "data-open")) === "1" && (await page.locator("#mic .square").count()) === 1;
  await page.tap("#stopwide");
  check("phone: a tap on the mic starts hands-free dictation; Stop ends it; nothing is sent",
    overlayOpen && (await page.getAttribute("#overlay", "data-open")) === "0" &&
      (await page.inputValue("#problem")).includes("red brake light is on") && agentCalls === 0);
  await page.fill("#problem", "");

  const serverOnly = await newContext(browser, PHONE, { sr: "server" });
  const serverPage = await openPage(serverOnly);
  await serverPage.waitForFunction((t) => document.getElementById("error").textContent === t, c.dictation_server_only, { timeout: 5000 }).catch(() => {});
  check("phone: the mic is shown, dimmed and disabled when only server recognition exists",
    await serverPage.isVisible("#mic") && await serverPage.isDisabled("#mic") &&
      (await serverPage.textContent("#error")) === c.dictation_server_only);
  await serverOnly.close();

  await page.tap("#menu");
  await page.selectOption("#customer", "C-1001");
  await closeDrawer(page, true);
  await page.fill("#problem", "red brake warning light");
  const answer = page.waitForResponse("**/api/agent");
  await page.tap("#ask");
  const json = await (await answer).json();
  await waitAssistants(page, 1);
  const bubble = await box("#thread .row.assistant >> nth=-1 >> .bubble");
  check("phone: a full ask round trip",
    (await last(page).getAttribute("data-status")) === "not_drivable" &&
      (await lastText(page, ".badge")) === CFG.badges.not_drivable &&
      (await lastText(page)).includes("carmind verdict: " + json.verdict.text) && (await lastText(page)).includes("Do not drive.") &&
      bubble.x + bubble.width <= 390 && await noHScroll());
  await last(page).locator("summary").tap();
  check("phone: Details opens and stays within the screen",
    (await last(page).locator("details.more[open]").count()) === 1 && (await last(page).locator(".d-tools li").count()) === 4 &&
      (await lastText(page, ".d-dispatch")).includes("Tow to dealer") && await noHScroll());

  await page.route("**/api/agent", (route) => route.abort());
  await page.fill("#problem", "check engine light");
  await page.tap("#ask");
  await waitAssistants(page, 2);
  check("phone: the FAILED bubble",
    (await lastText(page)).includes(FAILED) && (await last(page).getAttribute("data-status")) === "" &&
      (await last(page).locator(".badge").count()) === 0 && (await page.textContent("#error")) === "Could not reach the service." &&
      await noHScroll());
  await page.unroute("**/api/agent");

  await page.tap("#menu");
  await page.selectOption("#lang", "he-IL");
  const rtlOpen = await noHScroll();
  const asideRtl = await box("aside");
  await closeDrawer(page, true);
  const dirs = await page.evaluate(() => Array.from(document.querySelectorAll("#thread .row.user .bubble, #thread .bubble .reply"))
    .every((n) => n.getAttribute("dir") === "auto"));
  check("phone: right to left stays on the screen",
    (await page.getAttribute("#app", "dir")) === "rtl" && dirs && rtlOpen && await noHScroll() &&
      Math.abs(asideRtl.x + asideRtl.width - 390) <= 1);
  await page.tap("#menu");
  await page.selectOption("#lang", "en-US");
  const text = await page.evaluate(() => document.body.innerText);
  check("phone: page text is ASCII", /^[\x00-\x7F]*$/.test(text));
  await closeDrawer(page, true);
  check("phone: no page errors", pageErrors.length === errorsBefore, pageErrors.slice(errorsBefore).join("; "));
  await context.close();
}

// On an address that is not secure (the service's own name), the page says so and keeps the mic and saves off.
async function insecure(browser) {
  const context = await newContext(browser, DESKTOP, {}, SERVE_URL);
  const page = await openPage(context, SERVE_URL);
  const c = CFG.copy;
  await page.waitForTimeout(500);
  check("on a non-secure address the page says so and keeps the mic and saves off",
    (await page.evaluate(() => window.isSecureContext)) === false &&
      (await page.textContent("#error")) === c.insecure_context && await page.isDisabled("#mic") &&
      (await page.textContent("#saves-state")) === c.insecure_context && (await page.isHidden("#pass")) &&
      await page.isDisabled("#import"));
  await context.close();
}

async function main() {
  await waitForService();
  const proxy = await startProxy();
  CFG = await (await fetch(`${PAGE_URL}/api/config`)).json();
  const browser = await chromium.launch({
    args: ["--disable-dev-shm-usage", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
  });
  await core(browser);
  await phone(browser);
  await insecure(browser);
  await runSpeech(browser);
  await runSaves(browser);
  check("every request URL starts with PAGE_URL", offPage.length === 0, offPage.slice(0, 5).join(", "));
  check("no page errors", pageErrors.length === 0, pageErrors.join("; "));

  await browser.close();
  proxy.close();
  console.log(failed ? "\n[ui-e2e] FAILURES, see above" : "\n[ui-e2e] ALL PASS");
  process.exit(failed ? 1 : 0);
}

main().catch((err) => {
  console.log(`[ui-e2e] FATAL: ${err.message}`);
  process.exit(1);
});
