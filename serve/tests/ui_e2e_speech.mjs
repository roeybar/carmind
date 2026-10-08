// Dictation and read-aloud on the operator page, with stand-in speech engines (imported by ui_e2e.mjs).
import { CFG, ask, check, closeDrawer, last, newContext, openDrawer, openPage } from "./ui_e2e.mjs";

const errorIs = (page, text, ms = 6000) =>
  page.waitForFunction((t) => document.getElementById("error").textContent === t, text, { timeout: ms }).then(() => true, () => false);

async function withPage(browser, speech, body) {
  const context = await newContext(browser, undefined, speech);
  const page = await openPage(context);
  try { await body(page, context); } finally { await context.close(); }
}

const micOff = async (page) => (await page.isDisabled("#mic")) && (await page.textContent("#hint")) === "";

export async function runSpeech(browser) {
  const c = CFG.copy;

  await withPage(browser, { sr: "none" }, async (page) => {
    check("speech: no SR gives dictation_none and a disabled mic", await errorIs(page, c.dictation_none) && await micOff(page));
  });
  await withPage(browser, { sr: "server" }, async (page) => {
    check("speech: SR without local processing gives the dictation_server_only text exactly",
      await errorIs(page, c.dictation_server_only) && await micOff(page));
  });
  await withPage(browser, { available: { "he-IL": "unavailable" } }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await openDrawer(page);
    await page.selectOption("#lang", "he-IL");
    const serverOnly = await errorIs(page, c.dictation_server_only);
    const calls = await page.evaluate(() => window.__speech.available);
    check("speech: available unavailable for he-IL gives the dictation_server_only text, asked with langs [he-IL]",
      serverOnly && await micOff(page) && calls.some((o) => JSON.stringify(o.langs) === '["he-IL"]' && o.processLocally === true));
  });

  for (const how of ["reject", "throw", "hang"]) {
    await withPage(browser, { available: { "*": how } }, async (page) => {
      check(`speech: a ${how === "hang" ? "never-resolving" : how === "throw" ? "throwing" : "rejecting"} available() gives dictation_local_failed and a disabled mic`,
        await errorIs(page, c.dictation_local_failed, 8000) && await micOff(page));
    });
  }
  await withPage(browser, { available: { "*": "hold" } }, async (page) => {
    await page.fill("#problem", "red brake warning light");
    await page.click("#ask");
    await page.waitForSelector("#thread .row.assistant", { timeout: 20000 });
    check("speech: boot renders before detection resolves",
      (await page.locator("#thread .row.assistant").count()) === 1 && await page.isDisabled("#mic"));
  });
  await withPage(browser, { available: { "en-US": "hold", "he-IL": "unavailable" } }, async (page) => {
    await openDrawer(page);
    await page.selectOption("#lang", "he-IL");
    await errorIs(page, c.dictation_server_only);
    await page.evaluate(() => window.__speech.release["en-US"]("available"));
    await page.waitForTimeout(300);
    check("speech: a slow en-US detection that resolves after a switch to he-IL does not enable the mic",
      await page.isDisabled("#mic") && (await page.textContent("#error")) === c.dictation_server_only);
  });

  await withPage(browser, {}, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    let agentCalls = 0;
    page.on("request", (req) => { if (req.url().endsWith("/api/agent")) { agentCalls++; } });
    const mic = await page.locator("#mic").boundingBox();
    const x = mic.x + mic.width / 2, y = mic.y + mic.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.waitForTimeout(400);
    const heldOpen = (await page.getAttribute("#overlay", "data-open")) === "1";
    await page.mouse.move(x, y - 80, { steps: 5 });
    const lockedBySlide = (await page.locator("#mic .square").count()) === 1;
    await page.mouse.up();
    await page.waitForTimeout(200);
    const stillOpen = (await page.getAttribute("#overlay", "data-open")) === "1";
    await page.click("#stopwide");
    const closed = (await page.getAttribute("#overlay", "data-open")) === "0";
    const started = await page.evaluate(() => window.__speech.started);
    check("speech: the overlay opens on hold, locks on a slide or tap, and Stop and review closes it",
      heldOpen && lockedBySlide && stillOpen && closed);
    check("speech: processLocally === true and lang are set before start",
      started.length === 1 && started[0].processLocally === true && started[0].lang === "en-US");
    check("speech: dictation fills #problem and sends no /api/agent request",
      (await page.inputValue("#problem")) === "red brake light is on" && agentCalls === 0);
  });

  await withPage(browser, { emit: "a".repeat(4100) }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    await page.waitForFunction(() => document.getElementById("heardBox").textContent.length > 4000, null, { timeout: 5000 });
    await page.click("#stopwide");
    check("speech: dictation is clamped to 4000 characters", (await page.inputValue("#problem")).length === 4000);
  });

  await withPage(browser, { sr: "webkit-local" }, async (page) => {
    const on = await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 }).then(() => true, () => false);
    await withPage(browser, { sr: "webkit-server" }, async (other) => {
      check("speech: the webkit-only constructor with processLocally enables dictation; one without disables it",
        on && await errorIs(other, c.dictation_server_only) && await other.isDisabled("#mic"));
    });
  });

  await withPage(browser, { available: { "*": "downloadable" } }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    const installing = await errorIs(page, c.dictation_installing, 2000);
    check("speech: downloadable calls install and shows dictation_installing",
      installing && (await page.evaluate(() => window.__speech.install.length)) === 1 &&
        (await page.getAttribute("#overlay", "data-open")) !== "1");
  });
  await withPage(browser, { available: { "*": "downloadable" }, install: "reject" }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    check("speech: a failing install() gives dictation_local_failed", await errorIs(page, c.dictation_local_failed) && await micOff(page));
  });
  await withPage(browser, { error: "not-allowed" }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    check("speech: not-allowed gives dictation_denied", await errorIs(page, c.dictation_denied) && await micOff(page));
  });
  await withPage(browser, { error: "service-not-allowed" }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    check("speech: service-not-allowed gives dictation_local_failed", await errorIs(page, c.dictation_local_failed) && await micOff(page));
  });
  await withPage(browser, { end: true }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    const stopped = await errorIs(page, c.dictation_stopped);
    check("speech: a recogniser that ends on its own stops dictation and keeps the text",
      stopped && (await page.getAttribute("#overlay", "data-open")) === "0" &&
        (await page.inputValue("#problem")) === "red brake light is on" && !(await page.isDisabled("#mic")));
  });
  await withPage(browser, { available: { "en-US": "downloadable", "he-IL": "available" }, install: "hold" }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    await errorIs(page, c.dictation_installing);
    await openDrawer(page);
    await page.selectOption("#lang", "he-IL");
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 }).catch(() => {});
    await page.evaluate(() => window.__speech.releaseInstall(false));
    await page.waitForTimeout(400);
    check("speech: an en-US install() that fails after a switch to he-IL leaves he-IL dictation on",
      !(await page.isDisabled("#mic")) && (await page.textContent("#error")) !== c.dictation_local_failed);
  });
  await withPage(browser, { keepFalse: true }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    await page.click("#mic", { force: true });
    check("speech: a recogniser that does not keep processLocally is refused with the dictation_server_only text",
      await errorIs(page, c.dictation_server_only) && await micOff(page) &&
        (await page.evaluate(() => window.__speech.started.length)) === 0);
  });

  // Overlapping dictation sessions leave no microphone track or audio context running.
  await withPage(browser, { media: "hold" }, async (page) => {
    await page.waitForFunction(() => !document.getElementById("mic").disabled, null, { timeout: 6000 });
    const media = () => page.evaluate(() => {
      const m = window.__speech.media;
      return { tracks: m.tracks.map((t) => ({ n: t.n, stopped: t.stopped })), contexts: m.contexts.map((x) => x.closed) };
    });
    await page.click("#mic", { force: true });
    await page.waitForFunction(() => window.__speech.media.asked === 1, null, { timeout: 6000 });
    await page.click("#stopwide");
    await page.click("#mic", { force: true });
    await page.waitForFunction(() => window.__speech.media.asked === 2, null, { timeout: 6000 });
    await page.evaluate(() => window.__speech.media.release[0]());
    await page.waitForTimeout(200);
    const lateA = await media();
    const bOpen = (await page.getAttribute("#overlay", "data-open")) === "1";
    await page.evaluate(() => window.__speech.media.release[1]());
    await page.waitForTimeout(200);
    const running = await media();
    await page.click("#stopwide");
    await page.waitForTimeout(200);
    const after = await media();
    check("speech: a microphone that arrives after its session stopped is stopped at once, and the next session runs",
      lateA.tracks.length === 1 && lateA.tracks[0].n === 0 && lateA.tracks[0].stopped && lateA.contexts.length === 0 && bOpen &&
        running.tracks.length === 2 && !running.tracks[1].stopped && running.contexts.length === 1 && !running.contexts[0],
      JSON.stringify({ lateA, running }));
    check("speech: after the last stop every microphone track is stopped and every audio context closed",
      after.tracks.length === 2 && after.tracks.every((t) => t.stopped) && after.contexts.length === 1 && after.contexts.every((x) => x),
      JSON.stringify(after));
  });

  await withPage(browser, {}, async (page) => {
    await ask(page, { customer: "C-1001", problem: "red brake warning light", status: "not_drivable" });
    await last(page).locator(".speak").click();
    const spoken = await page.evaluate(() => window.__speech.spoken);
    const reply = await last(page).locator(".reply").textContent();
    check("speech: read-aloud speaks the exact bubble text with lang en-US and a local voice",
      spoken.length === 1 && spoken[0].text === reply && spoken[0].lang === "en-US" && spoken[0].local &&
        reply.includes("Do not drive."));
    await closeDrawer(page);
  });
  await withPage(browser, { voices: [{ name: "Remote English", lang: "en-US", localService: false }] }, async (page) => {
    await ask(page, { customer: "C-1001", problem: "red brake warning light", status: "not_drivable" });
    await last(page).locator(".speak").click();
    check("speech: only remote voices gives read_aloud_none",
      await errorIs(page, c.read_aloud_none) && (await page.evaluate(() => window.__speech.spoken.length)) === 0);
  });
}
