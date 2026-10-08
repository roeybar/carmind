// Encrypted browser saves, export and import on the operator page, and the browser stress (imported by ui_e2e.mjs).
import { webcrypto as wc } from "node:crypto";
import { readFile } from "node:fs/promises";
import { CFG, FAILED, LAST_RESORT, PAGE_URL, ask, check, closeDrawer, last, lastText, newContext, openDrawer, openPage, waitAssistants } from "./ui_e2e.mjs";

const PASS = "correct horse battery";
const enc = new TextEncoder();
const b64 = (bytes) => Buffer.from(bytes).toString("base64");
const unb64 = (text) => new Uint8Array(Buffer.from(text, "base64"));
const hexId = () => Buffer.from(wc.getRandomValues(new Uint8Array(8))).toString("hex");

function aad(env) {
  return enc.encode(JSON.stringify([env.format, env.v, env.kdf.name, env.kdf.iterations, env.kdf.salt, env.cipher.name, env.cipher.iv]));
}

async function aesKey(pass, salt, iterations) {
  const base = await wc.subtle.importKey("raw", enc.encode(pass), "PBKDF2", false, ["deriveKey"]);
  return wc.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, base, { name: "AES-GCM", length: 256 },
                             false, ["encrypt", "decrypt"]);
}

// An envelope built in node, the same way the page builds one.
export async function sealEnvelope(pass, plain, { iterations = 600000, salt = wc.getRandomValues(new Uint8Array(16)) } = {}) {
  const iv = wc.getRandomValues(new Uint8Array(12));
  const env = { format: "carmind-conversation", v: 1, kdf: { name: "PBKDF2-SHA256", iterations, salt: b64(salt) },
                cipher: { name: "AES-256-GCM", iv: b64(iv) }, ct: "" };
  const ct = await wc.subtle.encrypt({ name: "AES-GCM", iv, additionalData: aad(env) }, await aesKey(pass, salt, iterations),
                                     enc.encode(JSON.stringify(plain)));
  env.ct = b64(new Uint8Array(ct));
  return env;
}

export async function openFile(text, pass) {
  const env = JSON.parse(text);
  const key = await aesKey(pass, unb64(env.kdf.salt), env.kdf.iterations);
  const plain = await wc.subtle.decrypt({ name: "AES-GCM", iv: unb64(env.cipher.iv), additionalData: aad(env) }, key, unb64(env.ct));
  return JSON.parse(new TextDecoder().decode(plain));
}

// A valid conversation plaintext of n messages (user, then FAILED answers), optionally edited.
export function plainOf(n = 2, edit = null) {
  const now = Date.now(), messages = [];
  for (let i = 0; i < n; i++) {
    messages.push(i % 2 === 0 ? { id: hexId(), role: "user", created_at: now, text: "check engine light" }
      : { id: hexId(), role: "assistant", created_at: now, text: FAILED, kind: "failed", status: "",
          details: { error: "Could not reach the service.", customer_id: null } });
  }
  const plain = { format: "carmind-conversation-data", v: 1, conversation: {
    id: hexId(), title: "check engine light", created_at: now, updated_at: now,
    caller: { customer_id: "", caller_number: "", plate: "", after_hours: false }, imported: false, messages } };
  if (edit) { edit(plain); }
  return plain;
}

export async function sealFile(pass, { messages = 2, edit = null } = {}) {
  return JSON.stringify(await sealEnvelope(pass, plainOf(messages, edit)));
}

async function records(page) {
  return page.evaluate(() => new Promise((resolve) => {
    const req = indexedDB.open("carmind", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("conversations");
    req.onsuccess = () => {
      const db = req.result, tx = db.transaction("conversations"), store = tx.objectStore("conversations");
      const keys = store.getAllKeys(), values = store.getAll();
      tx.oncomplete = () => { db.close(); resolve(keys.result.map((k, i) => [k, values.result[i]])); };
    };
  }));
}

async function setPass(page, pass = PASS) {
  await openDrawer(page);
  await page.fill("#pass", pass);
  await page.fill("#pass2", pass);
  await page.click("#set-pass");
  await page.waitForFunction(() => document.getElementById("pass").hidden, null, { timeout: 20000 });
  await closeDrawer(page);
}

async function unlock(page, pass = PASS) {
  await page.waitForFunction(() => !document.getElementById("unlock").hidden, null, { timeout: 20000 });
  await openDrawer(page);
  await page.fill("#pass", pass);
  await page.click("#unlock");
}

async function unlocked(page, pass = PASS) {
  await unlock(page, pass);
  await page.waitForFunction(() => document.getElementById("unlock").hidden, null, { timeout: 30000 });
  await closeDrawer(page);
}

const waitSaved = (page) => page.waitForFunction(() => document.querySelectorAll(".chat .not-saved").length === 0, null,
                                                 { timeout: 20000 }).then(() => true, () => false);
const rowHidden = (page) => page.evaluate(() => document.getElementById("import-row").hidden);
const openId = (page) => page.getAttribute('.chat[data-active="1"]', "data-id");
const errorHas = (page, text, ms = 8000) =>
  page.waitForFunction((t) => document.getElementById("error").textContent.includes(t), text, { timeout: ms }).then(() => true, () => false);

async function sendMore(page, problem) {
  const n = await page.locator("#thread .row.assistant").count();
  await page.fill("#problem", problem);
  await page.click("#ask");
  await waitAssistants(page, n + 1);
}

async function download(page, trigger) {
  const [dl] = await Promise.all([page.waitForEvent("download", { timeout: 15000 }), trigger()]);
  return { name: dl.suggestedFilename(), text: await readFile(await dl.path(), "utf8") };
}
const setVal = (page, id, value) => page.evaluate(([i, v]) => { document.getElementById(i).value = v; }, [id, value]);
const attrHidden = (page, id) => page.evaluate((i) => document.getElementById(i).hidden, id);
const clickIn = (page, sel) => page.evaluate((s) => document.querySelector(s).click(), sel);
const exportOf = (page, id) => download(page, () => clickIn(page, `.chat[data-id="${id}"] .exp`));

async function importText(page, text, name = "file.json") {
  await page.setInputFiles("#import-file", { name, mimeType: "application/json", buffer: Buffer.from(text) });
}

async function threadOf(page) {
  return page.evaluate(() => Array.from(document.querySelectorAll("#thread .row")).map((r) => ({
    role: r.classList.contains("user") ? "user" : "assistant", status: r.getAttribute("data-status") || "",
    text: r.classList.contains("user") ? r.querySelector(".bubble").textContent : r.querySelector(".reply").textContent,
    more: r.querySelector("details.more") ? r.querySelector("details.more").textContent : null,
  })));
}

export async function runSaves(browser) {
  const c = CFG.copy;

  // SV1-SV3, the IV checks and SV8/SV9/SV9b: one browser profile across reloads.
  {
    const context = await newContext(browser);
    let page = await openPage(context);
    await ask(page, { customer: "C-1001", plate: "QZXPLATE", problem: "red brake warning light QZXCANARY" });
    await openDrawer(page);
    check("SV1: not saved shows with 0 records",
      (await page.locator(".chat .not-saved").count()) === 1 && (await records(page)).length === 0);
    await page.fill("#pass", "short12");
    await page.fill("#pass2", "short12");
    await page.click("#set-pass");
    const short = (await page.textContent("#saves-state")) === c.passphrase_short;
    await page.fill("#pass", PASS);
    await page.fill("#pass2", PASS + "x");
    await page.click("#set-pass");
    const mismatch = (await page.textContent("#saves-state")) === c.passphrase_mismatch;
    const warning = await page.isVisible("#pass-warning") && (await page.textContent("#pass-warning")) === c.passphrase_warning;
    await closeDrawer(page);
    await setPass(page);
    await waitSaved(page);
    let recs = await records(page);
    const env = recs.length === 1 ? JSON.parse(recs[0][1]) : null;
    check("SV2: a short or mismatched passphrase is refused, the warning shows, one envelope record is written",
      short && mismatch && warning && env && env.format === "carmind-conversation" && env.kdf.iterations === 600000 &&
        !recs[0][1].includes("QZXCANARY") && !recs[0][1].includes("QZXPLATE") && !recs[0][1].includes("555-0142") &&
        (await page.inputValue("#pass")) === "" && (await page.inputValue("#pass2")) === "");

    const ivs = [env.cipher.iv];
    const first = await openId(page);
    for (const p of ["check engine light", "ESC light is flashing"]) {
      await sendMore(page, p);
      await waitSaved(page);
      ivs.push(JSON.parse((await records(page)).find(([k]) => k === first)[1]).cipher.iv);
    }
    await ask(page, { customer: "C-1002", problem: "check engine light" });
    await waitSaved(page);
    const second = await openId(page);
    ivs.push(JSON.parse((await records(page)).find(([k]) => k === second)[1]).cipher.iv);
    const before = JSON.stringify(await records(page));
    const exp1 = await exportOf(page, first), exp2 = await exportOf(page, first);
    ivs.push(JSON.parse(exp1.text).cipher.iv, JSON.parse(exp2.text).cipher.iv);
    check("SV2: every IV is distinct (three writes of one record, two records, two exports)", new Set(ivs).size === ivs.length);
    const firstPlain = await openFile(exp1.text, PASS);
    check("SV8: the export filename, no canary in the file, the store unchanged",
      /^carmind-\d{8}-\d{4}\.json$/.test(exp1.name) && !exp1.text.includes("QZXCANARY") &&
        firstPlain.conversation.messages[0].text.includes("QZXCANARY") && JSON.stringify(await records(page)) === before);

    await importText(page, exp1.text);
    await page.waitForSelector("#thread .imported-notice", { timeout: 20000 });
    await waitSaved(page);
    recs = await records(page);
    check("SV9: importing our own file adds one conversation and leaves the original untouched",
      recs.length === 3 && JSON.stringify(recs.find(([k]) => k === first)) === JSON.stringify(JSON.parse(before).find(([k]) => k === first)) &&
        (await page.locator(".chat .imported").count()) === 1 &&
        (await page.textContent("#thread .imported-notice")) === c.imported_notice &&
        (await page.locator(`.chat[data-id="${first}"]`).count()) === 1 && !(await page.textContent("#error")).includes(c.save_conflict));

    // Untrusted ids are looked up as own properties only.
    const odd = { id: hexId(), role: "assistant", created_at: Date.now(), text: "carmind verdict: x", kind: "answer", status: "drivable",
      details: { source: null, note: "", provider: null, error: null, customer_id: "constructor",
                 dispatch: { action: "constructor", provider: null, destination: null, coverage: "standard", covered: false, steps: [] },
                 choice: null, context: null, tool_calls: [] } };
    await importText(page, JSON.stringify(await sealEnvelope(PASS, plainOf(2, (p) => { p.conversation.messages[1] = odd; }))));
    await page.waitForFunction(() => { const r = document.querySelector("#thread .row.assistant .reply"); return r && r.textContent === "carmind verdict: x"; },
                               null, { timeout: 20000 }).catch(() => {});
    await last(page).locator("details.more").evaluate((d) => { d.open = true; });
    const oddText = (await page.textContent("#customer-card")) + (await lastText(page, "details.more"));
    check("an imported conversation with customer_id and action \"constructor\" shows no Object and no function",
      (await lastText(page, ".reply")) === "carmind verdict: x" && !oddText.includes("Object") && !oddText.includes("function"), oddText);
    await waitSaved(page);

    // SV3: after a reload the store is locked.
    const shownBefore = await (async () => {
      await openDrawer(page);
      await page.click(`.chat[data-id="${first}"]`);
      return threadOf(page);
    })();
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    const bytes = JSON.stringify(await records(page));
    await unlock(page, "not the passphrase");
    await page.waitForFunction((t) => document.getElementById("saves-state").textContent.startsWith(t), c.unlock_wrong, { timeout: 30000 });
    const refused = JSON.stringify(await records(page)) === bytes && await page.isVisible("#force") &&
      (await page.inputValue("#pass")) === "";
    await closeDrawer(page);
    await unlocked(page);
    await openDrawer(page);
    await page.click(`.chat[data-id="${first}"]`);
    const shownAfter = await threadOf(page);
    check("SV3: after a reload a wrong passphrase is refused, bytes unchanged; the right one restores the bubbles and Details",
      refused && JSON.stringify(shownAfter) === JSON.stringify(shownBefore) && shownAfter.some((r) => r.more));
    const sorted = JSON.parse(bytes).map(([k]) => k).sort();
    const firstSalt = JSON.parse(JSON.parse(bytes).find(([k]) => k === sorted[0])[1]).kdf.salt;
    await sendMore(page, "check engine light");
    await waitSaved(page);
    check("SV2: a record written after Unlock carries the salt of the first opened record",
      JSON.parse((await records(page)).find(([k]) => k === first)[1]).kdf.salt === firstSalt &&
        (await page.inputValue("#pass")) === "");

    // SV9b: a session-1 export opens with no prompt after the reload; a flipped byte asks for the passphrase.
    await importText(page, exp2.text);
    await page.waitForSelector("#thread .imported-notice", { timeout: 20000 });
    const noPrompt = await rowHidden(page);
    const bad = JSON.parse(exp2.text);
    const ct = unb64(bad.ct);
    ct[3] ^= 1;
    bad.ct = b64(ct);
    await importText(page, JSON.stringify(bad));
    check("SV9b: our own earlier export opens with no passphrase prompt; a flipped byte gives import_not_session",
      noPrompt && await errorHas(page, c.import_not_session) && !(await rowHidden(page)));

    // SV10: shape failures need no passphrase; crypto failures ask for one, then fail.
    const good = await sealEnvelope(PASS, plainOf());
    const shapes = {
      "iterations 599999": (e) => { e.kdf.iterations = 599999; },
      "iterations 2000001": (e) => { e.kdf.iterations = 2000001; },
      "another format": (e) => { e.format = "other"; },
      "v 2": (e) => { e.v = 2; },
      "another kdf": (e) => { e.kdf.name = "scrypt"; },
      "another cipher": (e) => { e.cipher.name = "AES-128-GCM"; },
      "a 15-byte salt": (e) => { e.kdf.salt = b64(new Uint8Array(15)); },
      "a 13-byte IV": (e) => { e.cipher.iv = b64(new Uint8Array(13)); },
      "a ciphertext under 16 bytes": (e) => { e.ct = b64(new Uint8Array(15)); },
      "a salt that is not base64": (e) => { e.kdf.salt = "!!not base64!!!!"; },
    };
    let shapeOk = true;
    for (const [label, edit] of Object.entries(shapes)) {
      const e = JSON.parse(JSON.stringify(good));
      edit(e);
      await page.evaluate(() => { document.getElementById("error").textContent = ""; });
      await importText(page, JSON.stringify(e));
      const ok = await errorHas(page, c.import_not_carmind) && await rowHidden(page);
      if (!ok) { console.log(`        (shape case failed: ${label})`); }
      shapeOk = shapeOk && ok;
    }
    check("SV10: shape failures give import_not_carmind with no passphrase prompt", shapeOk);
    const cryptoCases = {
      "a flipped ciphertext byte": (e) => { const b = unb64(e.ct); b[0] ^= 1; e.ct = b64(b); },
      "a truncated ciphertext": (e) => { e.ct = b64(unb64(e.ct).slice(0, 20)); },
      "another valid salt": (e) => { e.kdf.salt = b64(wc.getRandomValues(new Uint8Array(16))); },
    };
    let cryptoOk = true;
    for (const [label, edit] of Object.entries(cryptoCases)) {
      const e = JSON.parse(JSON.stringify(good));
      edit(e);
      await page.evaluate(() => { document.getElementById("error").textContent = ""; });
      await importText(page, JSON.stringify(e));
      const asked = await errorHas(page, c.import_not_session);
      await openDrawer(page);
      await page.fill("#import-pass", PASS);
      await page.evaluate(() => { document.getElementById("error").textContent = ""; });
      await page.click("#import-go");
      const wrong = await errorHas(page, c.import_wrong, 15000) && (await page.inputValue("#import-pass")) === "";
      await closeDrawer(page);
      if (!(asked && wrong)) { console.log(`        (crypto case failed: ${label})`); }
      cryptoOk = cryptoOk && asked && wrong;
    }
    check("SV10: crypto failures give import_not_session, then import_wrong after the passphrase", cryptoOk);

    // SV11: valid crypto around a bad plaintext.
    const badPlains = {
      "role system": (p) => { p.conversation.messages[0].role = "system"; },
      "a 4001-character text": (p) => { p.conversation.messages[0].text = "x".repeat(4001); },
      "401 messages": (p) => { const m = p.conversation.messages; while (m.length < 401) { m.push({ ...m[0], id: hexId() }); } },
      "status safe": (p) => { p.conversation.messages[1].status = "safe"; },
    };
    let plainOk = true;
    for (const [label, edit] of Object.entries(badPlains)) {
      const text = JSON.stringify(await sealEnvelope(PASS, plainOf(2, edit)));
      await page.evaluate(() => { document.getElementById("error").textContent = ""; });
      await importText(page, text);
      const ok = await errorHas(page, c.import_not_carmind, 15000);
      if (!ok) { console.log(`        (plaintext case failed: ${label})`); }
      plainOk = plainOk && ok;
    }
    const markup = "<img src=x onerror=alert(1)>";
    await importText(page, JSON.stringify(await sealEnvelope(PASS, plainOf(2, (p) => { p.conversation.messages[0].text = markup; }))));
    await page.waitForFunction((t) => { const b = document.querySelector("#thread .row.user .bubble"); return b && b.textContent === t; },
                               markup, { timeout: 20000 }).catch(() => {});
    check("SV11: a bad plaintext is not a carmind file; markup renders as text",
      plainOk && (await page.locator("#thread img").count()) === 0 &&
        (await page.locator("#thread .row.user .bubble").first().textContent()) === markup);

    // SV12: the size cap.
    const cap = CFG.limits.import_bytes;
    await importText(page, "x".repeat(cap + 1));
    const tooLarge = await errorHas(page, c.import_too_large);
    const small = JSON.stringify(await sealEnvelope(PASS, plainOf(2, (p) => { p.conversation.title = "padded"; })));
    const before12 = (await records(page)).length;
    await importText(page, small + " ".repeat(cap - 1 - Buffer.byteLength(small)));
    await page.waitForFunction(() => { const b = document.querySelector('.chat[data-active="1"] b'); return b && b.textContent === "padded"; },
                               null, { timeout: 20000 }).catch(() => {});
    check("SV12: a file of cap+1 bytes is too large; a padded valid file of cap-1 bytes imports",
      tooLarge && (await page.locator("#thread .imported-notice").count()) === 1 && await waitSaved(page) &&
        (await records(page)).length === before12 + 1);
    await context.close();
  }

  // SV4: Force leaves the old record byte-identical.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    await waitSaved(page);
    const before = JSON.stringify(await records(page));
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    await unlock(page, "another passphrase");
    await page.waitForFunction(() => !document.getElementById("force").hidden, null, { timeout: 30000 });
    await setVal(page, "pass", "another passphrase");
    await setVal(page, "pass2", "another passphrase");
    await clickIn(page, "#force");
    await page.waitForFunction(() => !document.getElementById("locked-count").hidden, null, { timeout: 30000 }).catch(() => {});
    check("SV4: Force leaves the old record byte-identical and counts it as locked",
      JSON.stringify(await records(page)) === before &&
        (await page.textContent("#locked-count")) === c.locked_count.replace("{n}", "1"));
    await context.close();
  }

  // SV17: Force follows the Set rules (8 characters, typed twice); records it cannot read are split into locked and damaged.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    await waitSaved(page);
    await page.evaluate(() => new Promise((resolve) => {
      const req = indexedDB.open("carmind", 1);
      req.onsuccess = () => {
        const tx = req.result.transaction("conversations", "readwrite");
        tx.objectStore("conversations").put("not an envelope", "00000000000000aa");
        tx.oncomplete = () => { req.result.close(); resolve(); };
      };
    }));
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    const before = JSON.stringify(await records(page));
    const NEW = "second passphrase";
    await unlock(page, NEW);
    await page.waitForFunction(() => !document.getElementById("force").hidden, null, { timeout: 30000 });
    const fields = !(await attrHidden(page, "pass")) && !(await attrHidden(page, "pass2")) && !(await attrHidden(page, "pass-warning")) &&
      (await page.inputValue("#pass")) === "" && (await page.inputValue("#pass2")) === "";
    check("SV17: the Force offer shows both passphrase fields, empty, and the warning", fields);
    await setVal(page, "pass", "short12");
    await setVal(page, "pass2", "short12");
    await clickIn(page, "#force");
    await page.waitForTimeout(300);
    check("SV17: Force with 7 characters gives passphrase_short and writes nothing",
      (await page.textContent("#saves-state")) === c.passphrase_short && JSON.stringify(await records(page)) === before &&
        !(await attrHidden(page, "force")));
    await setVal(page, "pass", NEW);
    await setVal(page, "pass2", NEW + "x");
    await clickIn(page, "#force");
    await page.waitForTimeout(300);
    check("SV17: Force with mismatched entries gives passphrase_mismatch and writes nothing",
      (await page.textContent("#saves-state")) === c.passphrase_mismatch && JSON.stringify(await records(page)) === before);
    await setVal(page, "pass", NEW);
    await setVal(page, "pass2", NEW);
    await clickIn(page, "#force");
    await page.waitForFunction(() => !document.getElementById("locked-count").hidden, null, { timeout: 30000 }).catch(() => {});
    const counts = [await page.textContent("#locked-count"), await page.textContent("#damaged-count"), await attrHidden(page, "damaged-count")];
    check("SV17: both passphrase fields are empty after Force",
      (await page.inputValue("#pass")) === "" && (await page.inputValue("#pass2")) === "");
    await closeDrawer(page);
    await ask(page, { customer: "C-1002", problem: "check engine light" });
    await waitSaved(page);
    const added = (await records(page)).filter(([k]) => !JSON.parse(before).some(([b]) => b === k));
    const opened = added.length === 1 ? await openFile(added[0][1], NEW).then(() => true, () => false) : false;
    check("SV17: valid Force entries count one locked and one damaged record, and new saves open under the new passphrase",
      counts[0] === c.locked_count.replace("{n}", "1") && counts[1] === c.damaged_count.replace("{n}", "1") && !counts[2] && opened,
      JSON.stringify(counts));
    await context.close();
  }
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    await waitSaved(page);
    const key = (await records(page))[0][0];
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    await unlock(page, "not the passphrase");
    await page.waitForFunction(() => !document.getElementById("force").hidden, null, { timeout: 30000 });
    await setVal(page, "pass", PASS);
    await setVal(page, "pass2", PASS);
    await clickIn(page, "#force");
    await page.waitForFunction(() => document.getElementById("unlock").hidden, null, { timeout: 30000 }).catch(() => {});
    check("SV17: a Force entry that opens a record behaves as an unlock",
      (await page.locator(`.chat[data-id="${key}"]`).count()) === 1 && (await attrHidden(page, "locked-count")) &&
        (await page.textContent("#saves-state")) === c.saved_state);
    await context.close();
  }

  // SV20: an Unlock in the Force offer state leaves neither passphrase field filled.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    await waitSaved(page);
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    await unlock(page, "not the passphrase");
    await page.waitForFunction(() => !document.getElementById("force").hidden, null, { timeout: 30000 });
    await setVal(page, "pass", PASS);
    await setVal(page, "pass2", "typed here too");
    await clickIn(page, "#unlock");
    await page.waitForFunction(() => document.getElementById("unlock").hidden, null, { timeout: 30000 }).catch(() => {});
    check("SV20: an Unlock in the Force offer state empties both passphrase fields",
      (await attrHidden(page, "unlock")) && (await page.inputValue("#pass")) === "" && (await page.inputValue("#pass2")) === "");
    await context.close();
  }

  // SV21: Force and Unlock pressed together: only the first runs, and the saves follow its passphrase.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    await waitSaved(page);
    const oldKey = (await records(page))[0][0];
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    await unlock(page, "not the passphrase");
    await page.waitForFunction(() => !document.getElementById("force").hidden, null, { timeout: 30000 });
    const NEW = "the winning passphrase";
    await page.evaluate(([fresh, old]) => {
      const $ = (id) => document.getElementById(id);
      $("pass").value = fresh; $("pass2").value = fresh;
      $("force").click();
      $("pass").value = old;
      $("unlock").click();
    }, [NEW, PASS]);
    await page.waitForFunction(() => !document.getElementById("locked-count").hidden, null, { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(1500);
    const oldListed = (await page.locator(`.chat[data-id="${oldKey}"]`).count()) > 0;
    const locked = await page.textContent("#locked-count");
    await closeDrawer(page);
    await ask(page, { customer: "C-1002", problem: "check engine light WINNER" });
    await waitSaved(page);
    const fresh = (await records(page)).find(([k]) => k !== oldKey);
    const opens = fresh ? await openFile(fresh[1], NEW).then(() => true, () => false) : false;
    await page.reload();
    await unlocked(page, NEW);
    const reopened = fresh ? (await page.locator(`.chat[data-id="${fresh[0]}"]`).count()) === 1 : false;
    check("SV21: Force then Unlock during the Force record pass runs only Force; keys and counts follow its passphrase",
      !oldListed && locked === c.locked_count.replace("{n}", "1") && opens && reopened,
      JSON.stringify({ oldListed, locked, opens, reopened }));
    await context.close();
  }

  // SV19: unlocking 14 records under one salt derives the key once.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    const salt = wc.getRandomValues(new Uint8Array(16));
    const items = [];
    for (let i = 0; i < 14; i++) {
      const plain = plainOf(2);
      items.push([plain.conversation.id, JSON.stringify(await sealEnvelope(PASS, plain, { salt }))]);
    }
    await page.evaluate((list) => new Promise((resolve) => {
      const req = indexedDB.open("carmind", 1);
      req.onsuccess = () => {
        const tx = req.result.transaction("conversations", "readwrite");
        list.forEach(([k, v]) => tx.objectStore("conversations").put(v, k));
        tx.oncomplete = () => { req.result.close(); resolve(); };
      };
    }), items);
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    await page.evaluate(() => {
      window.__derives = 0;
      const derive = crypto.subtle.deriveKey.bind(crypto.subtle);
      crypto.subtle.deriveKey = (...args) => { window.__derives++; return derive(...args); };
    });
    await unlocked(page);
    const listed = await page.evaluate(() => document.querySelectorAll(".chat").length);
    const derives = await page.evaluate(() => window.__derives);
    check("SV19: unlocking 14 records under one salt derives the key once", derives === 1 && listed === 15, `${derives} derives, ${listed} rows`);
    await context.close();
  }

  // SV18: removing a conversation while its first save is in flight sticks.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await page.evaluate(() => {
      const encrypt = crypto.subtle.encrypt.bind(crypto.subtle);
      let first = true;
      crypto.subtle.encrypt = (...args) => {
        if (!first) { return encrypt(...args); }
        first = false;
        return new Promise((resolve) => setTimeout(resolve, 2500)).then(() => encrypt(...args));
      };
    });
    await ask(page, { customer: "C-1001", problem: "check engine light REMOVEME" });
    const id = await openId(page);
    await page.click("#new");
    const removed = await download(page, () => clickIn(page, `.chat[data-id="${id}"] .del`));
    await page.waitForTimeout(3500);
    const goneNow = !(await records(page)).some(([k]) => k === id);
    await page.reload();
    await page.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    check("SV18: removing a conversation while its first save is in flight sticks after a reload",
      removed.text.includes("carmind-conversation") && goneNow && !(await records(page)).some(([k]) => k === id));
    await context.close();
  }

  // SV5: a refused write, and Save to file from the notice; SV15: a validate failure; SV13: beforeunload.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await page.evaluate(() => {
      const put = IDBObjectStore.prototype.put;
      IDBObjectStore.prototype.put = function () {
        IDBObjectStore.prototype.put = put;
        throw new DOMException("refused", "QuotaExceededError");
      };
    });
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    const refused = await errorHas(page, c.save_refused);
    const file = await download(page, () => page.click("#error button"));
    check("SV5: a refused write shows save_refused and Save to file downloads", refused && file.text.includes("carmind-conversation"));

    const count = (await records(page)).length;
    await page.route("**/api/agent", async (route) => {
      const resp = await route.fetch();
      const body = await resp.json();
      body.tool_calls = Array.from({ length: 70 }, () => ({ name: "assess_warning_light", by: "model", summary: "x" }));
      await route.fulfill({ response: resp, json: body });
    });
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    check("SV15: an answer over the tool-call cap is shown and refused as save_invalid",
      (await last(page).locator(".d-tools li").count()) === 70 && await errorHas(page, c.save_invalid) &&
        (await records(page)).length === count);
    await page.unroute("**/api/agent");
    let invalidDownloads = 0;
    page.on("download", () => { invalidDownloads++; });
    await page.evaluate(() => { document.getElementById("error").textContent = ""; });
    await clickIn(page, '.chat[data-active="1"] .exp');
    const invalid = await errorHas(page, c.export_invalid);
    await page.waitForTimeout(800);
    check("export_invalid: a conversation that fails validation is not saved to a file", invalid && invalidDownloads === 0);
    await context.close();

    const ctx2 = await newContext(browser);
    const p2 = await ctx2.newPage();
    await p2.goto(`${PAGE_URL}/`);
    await p2.waitForFunction(() => document.getElementById("boot-failed").hidden, null, { timeout: 20000 });
    await ask(p2, { customer: "C-1001", problem: "check engine light" });
    let kind = null;
    p2.on("dialog", (d) => { kind = d.type(); d.accept().catch(() => {}); });
    await p2.close({ runBeforeUnload: true });
    await new Promise((resolve) => setTimeout(resolve, 1000));
    check("SV13: closing with an unsaved conversation raises a beforeunload dialog", kind === "beforeunload", String(kind));
    await ctx2.close();
  }

  // SV6: two pages on one record.
  {
    const context = await newContext(browser);
    const p1 = await openPage(context);
    await setPass(p1);
    await ask(p1, { customer: "C-1001", problem: "check engine light" });
    await waitSaved(p1);
    const shared = await openId(p1);
    const p2 = await openPage(context);
    await unlocked(p2);
    await openDrawer(p2);
    await p2.click(`.chat[data-id="${shared}"]`);
    await sendMore(p1, "red brake warning light PAGEONE");
    await waitSaved(p1);
    await sendMore(p2, "ESC light is flashing PAGETWO");
    const conflict = await errorHas(p2, c.save_conflict);
    await waitSaved(p2);
    const moved = await openId(p2);
    const recs = await records(p1);
    const one = await openFile(recs.find(([k]) => k === shared)[1], PASS);
    const two = await openFile(recs.find(([k]) => k === moved)[1], PASS);
    const texts = (p) => p.conversation.messages.map((m) => m.text).join("|");
    await p1.reload();
    await p2.reload();
    await unlocked(p1);
    await unlocked(p2);
    const listed = await p1.evaluate(() => Array.from(document.querySelectorAll(".chat")).map((r) => r.dataset.id));
    check("SV6: two pages on one record give save_conflict; both records decrypt to their own messages and are listed",
      conflict && moved !== shared && recs.length === 2 && texts(one).includes("PAGEONE") && !texts(one).includes("PAGETWO") &&
        texts(two).includes("PAGETWO") && !texts(two).includes("PAGEONE") && listed.includes(shared) && listed.includes(moved));
    await context.close();
  }

  // SV7, SV7b, SV14 and SV16.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await ask(page, { customer: "C-1001", problem: "check engine light" });
    const memory = await openId(page);
    await page.click("#new");
    let downloads = 0;
    page.on("download", () => { downloads++; });
    await clickIn(page, `.chat[data-id="${memory}"] .del`);
    const needs = await errorHas(page, c.export_needs_passphrase);
    await page.waitForTimeout(800);
    check("SV7b: with no passphrase, removing shows export_needs_passphrase and keeps the conversation",
      needs && downloads === 0 && (await page.locator(`.chat[data-id="${memory}"]`).count()) === 1);
    await setPass(page);
    await waitSaved(page);
    await page.route("**/api/agent", async (route) => { await new Promise((r) => setTimeout(r, 2000)); await route.continue(); });
    await page.click("#new");
    await page.fill("#problem", "red brake warning light LATEANSWER");
    await page.click("#ask");
    const pendingId = await openId(page);
    await page.click("#new");
    const pendingDisabled = await page.isDisabled(`.chat[data-id="${pendingId}"] .del`) &&
      await page.isDisabled(`.chat[data-active="1"] .del`);
    await page.waitForTimeout(2600);
    await page.unroute("**/api/agent");
    await waitSaved(page);
    const late = (await records(page)).find(([k]) => k === pendingId);
    const latePlain = late ? await openFile(late[1], PASS) : null;
    check("SV14: a late answer is saved in its own conversation's record",
      latePlain && latePlain.conversation.messages.length === 2 && latePlain.conversation.messages[1].kind === "answer" &&
        latePlain.conversation.messages[0].text.includes("LATEANSWER"));
    // The order is taken in the page: the download link's click, then the store's delete.
    await page.evaluate(() => {
      window.__order = [];
      const click = HTMLAnchorElement.prototype.click, del = IDBObjectStore.prototype.delete;
      HTMLAnchorElement.prototype.click = function () { if (this.download) { window.__order.push("download"); } return click.call(this); };
      IDBObjectStore.prototype.delete = function (key) { window.__order.push("removed"); return del.call(this, key); };
    });
    const removed = await download(page, () => clickIn(page, `.chat[data-id="${memory}"] .del`));
    await page.waitForFunction((id) => !document.querySelector(`.chat[data-id="${id}"]`), memory, { timeout: 15000 }).catch(() => {});
    const order = await page.evaluate(() => window.__order);
    check("SV7: remove downloads first, then removes; it is disabled while a request is pending and for the open one",
      pendingDisabled && JSON.stringify(order) === '["download","removed"]' && removed.text.includes("carmind-conversation") &&
        !(await records(page)).some(([k]) => k === memory));

    // SV16: Details survive a save, reload and unlock byte for byte.
    const crafted = [
      (b) => { b.choice.status = "rejected"; }, (b) => { b.choice.status = "failed"; },
      (b) => { b.choice.withheld = true; b.choice.reasoning = ""; }, (b) => { b.choice = null; b.error = "the agent turn failed."; },
      (b) => { b.context = { ...b.context, status: "rejected" }; }, (b) => { b.context = { ...b.context, status: "failed" }; },
      (b) => { b.context = { ...b.context, status: "none", flags: [] }; }, (b) => { b.dispatch = { error: "tool_failed" }; },
    ];
    const cases = [
      { customer: "C-1001", problem: "red brake warning light" },
      { customer: "C-1002", problem: CFG.examples[3] },
      { customer: "C-1002", problem: CFG.examples[4] },
      ...crafted.map((edit) => ({ customer: "C-1002", problem: CFG.examples[3], edit })),
      { customer: "C-1001", problem: "check engine light", abort: true },
      { customer: "C-1001", problem: "red brake warning light", reject: true },
    ];
    const seen = [];
    for (const k of cases) {
      if (k.edit) {
        await page.route("**/api/agent", async (route) => {
          const resp = await route.fetch();
          const body = await resp.json();
          k.edit(body);
          await route.fulfill({ response: resp, json: body });
        });
      } else if (k.abort) {
        await page.route("**/api/agent", (route) => route.abort());
      } else if (k.reject) {
        await page.route("**/api/agent", (route) => route.fulfill({ status: 400, contentType: "application/json",
          body: JSON.stringify({ error: "invalid request", precaution: "Do not drive until the car has been checked." }) }));
      }
      await ask(page, { customer: k.customer, problem: k.problem });
      if (k.edit || k.abort || k.reject) { await page.unroute("**/api/agent"); }
      const more = last(page).locator("details.more");
      if (await more.count()) { await more.evaluate((d) => { d.open = true; }); }
      seen.push([await openId(page), await last(page).getAttribute("data-status"), await lastText(page),
                 (await more.count()) ? await more.innerText() : (await more.count())]);
    }
    await waitSaved(page);
    const noRefusal = !(await page.textContent("#error")).includes(c.save_invalid) &&
      !(await page.textContent("#error")).includes(c.save_refused);
    await page.reload();
    await unlocked(page);
    let same = true;
    for (const [id, status, bubble, more] of seen) {
      await openDrawer(page);
      await page.click(`.chat[data-id="${id}"]`);
      const d = last(page).locator("details.more");
      if (await d.count()) { await d.evaluate((x) => { x.open = true; }); }
      const now = [id, await last(page).getAttribute("data-status"), await lastText(page), (await d.count()) ? await d.innerText() : (await d.count())];
      if (JSON.stringify(now) !== JSON.stringify([id, status, bubble, more])) { same = false; console.log(`        (SV16 differs: ${id})`); }
    }
    const failedRow = seen[seen.length - 2], rejectedRow = seen[seen.length - 1];
    check("SV16: Details survive a save, reload and unlock byte for byte (with FAILED and 400 cases)",
      same && noRefusal && failedRow[2] === FAILED && failedRow[3] === 0 &&
        rejectedRow[2] === "invalid request Do not drive until the car has been checked." && rejectedRow[3] === 0);
    await context.close();
  }

  // The service's last-resort 200 ("internal error") is stored as a FAILED message.
  {
    const context = await newContext(browser);
    const page = await openPage(context);
    await setPass(page);
    await page.route("**/api/agent", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(LAST_RESORT) }));
    await ask(page, { problem: "smoke from engine" });
    await waitSaved(page);
    const plain = await openFile((await exportOf(page, await openId(page))).text, PASS);
    const m = plain.conversation.messages[1];
    check("the last-resort 200 is stored as kind failed with the fixed precaution and request_failed",
      m.kind === "failed" && m.status === "" && m.text === c.failed && m.details.error === c.request_failed, JSON.stringify(m));
    await context.close();
  }

  await stress(browser);
}

function mulberry32(seed) {
  let a = seed;
  return () => {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// The state each page shows: every conversation's id, markers and thread.
async function shown(page) {
  return page.evaluate(async () => {
    const ids = Array.from(document.querySelectorAll("#chats .chat")).map((r) => r.dataset.id);
    const out = [];
    for (const id of ids) {
      const row = document.querySelector(`.chat[data-id="${id}"]`);
      if (!row) { continue; }
      const notSaved = !!row.querySelector(".not-saved"), imported = !!row.querySelector(".imported");
      row.click();
      await new Promise((r) => setTimeout(r, 0));
      const thread = Array.from(document.querySelectorAll("#thread .row")).map((r) => ({
        role: r.classList.contains("user") ? "user" : "assistant", status: r.getAttribute("data-status") || "",
        text: r.classList.contains("user") ? r.querySelector(".bubble").textContent : r.querySelector(".reply").textContent }));
      out.push({ id, notSaved, imported, thread });
    }
    return out;
  });
}

async function stress(browser) {
  const rand = mulberry32(7), pick = (list) => list[Math.floor(rand() * list.length)];
  const context = await newContext(browser);
  const p1 = await openPage(context);
  await setPass(p1);
  await ask(p1, { customer: "C-1001", problem: "stress seed check engine light" });
  await waitSaved(p1);
  const p2 = await openPage(context);
  await unlocked(p2);
  const pages = [p1, p2], inflight = new Map(pages.map((p) => [p, 0])), modes = new Map(pages.map((p) => [p, []]));
  // One route per page for the whole run; each send queues how its request is answered.
  for (const p of pages) {
    await p.route("**/api/agent", async (route) => {
      const mode = modes.get(p).shift() || "ok";
      if (mode === "abort") { await route.abort().catch(() => {}); return; }
      if (mode === "500") { await route.fulfill({ status: 500, contentType: "application/json", body: "{}" }).catch(() => {}); return; }
      if (mode === "slow") { await new Promise((z) => setTimeout(z, 1200)); }
      await route.continue().catch(() => {});
    });
  }
  for (const p of pages) {
    p.on("request", (r) => { if (r.url().endsWith("/api/agent")) { inflight.set(p, inflight.get(p) + 1); } });
    const done = (r) => { if (r.url().endsWith("/api/agent")) { inflight.set(p, inflight.get(p) - 1); } };
    p.on("requestfinished", done);
    p.on("requestfailed", done);
  }
  const expectFailed = new Set(), injected = new Set(), files = [];
  let k = 0;
  for (let op = 0; op < 60; op++) {
    const page = pick(pages), r = rand();
    try {
      if (r < 0.45 || r >= 0.9) {
        if (await page.isDisabled("#ask")) { await page.click("#new"); }
        const problem = `stress ${++k} check engine light`;
        const mode = pick(["ok", "abort", "500", "slow"]);
        modes.get(page).push(mode);
        if (mode === "abort" || mode === "500") { expectFailed.add(problem); }
        if (r >= 0.9) {
          const id = await openId(page);
          injected.add(id);
          await page.evaluate((target) => {
            const put = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function (value, key) {
              if (key === target) { IDBObjectStore.prototype.put = put; throw new DOMException("refused", "QuotaExceededError"); }
              return put.call(this, value, key);
            };
          }, id);
        }
        await page.fill("#problem", problem);
        await page.click("#ask");
        if (mode !== "slow") { await page.waitForTimeout(150); }
      } else if (r < 0.6) {
        const ids = await page.evaluate(() => Array.from(document.querySelectorAll("#chats .chat")).map((x) => x.dataset.id));
        if (ids.length) { await clickIn(page, `.chat[data-id="${pick(ids)}"]`); }
      } else if (r < 0.7) {
        await page.click("#new");
      } else if (r < 0.8) {
        const ids = await page.evaluate(() => Array.from(document.querySelectorAll("#chats .chat"))
          .filter((x) => !x.querySelector("em").textContent.endsWith(" 0 messages")).map((x) => x.dataset.id));
        if (ids.length) { files.push((await exportOf(page, pick(ids))).text); }
      } else if (files.length) {
        await importText(page, pick(files));
        await page.waitForTimeout(300);
      }
    } catch (err) {
      check(`ui stress: operation ${op}`, false, err.message);
    }
  }
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline && [...inflight.values()].some((n) => n > 0)) { await new Promise((z) => setTimeout(z, 200)); }
  await new Promise((z) => setTimeout(z, 3000));

  const views = [await shown(p1), await shown(p2)];
  const recs = await records(p1);
  const plains = new Map();
  for (const [key, text] of recs) { plains.set(key, await openFile(text, PASS).catch(() => null)); }
  const problems = [];
  const owner = new Map();
  for (const view of views) {
    for (const conv of view) {
      const t = conv.thread;
      for (let i = 0; i < t.length; i++) {
        if (t[i].role === "user" && !(t[i + 1] && t[i + 1].role === "assistant")) { problems.push(`${conv.id}: no answer at ${i}`); }
        if (t[i].role === "assistant" && !(t[i - 1] && t[i - 1].role === "user")) { problems.push(`${conv.id}: extra answer at ${i}`); }
        if (t[i].role === "user" && expectFailed.has(t[i].text) && t[i + 1] && (t[i + 1].text !== FAILED || t[i + 1].status !== "")) {
          problems.push(`${conv.id}: ${t[i].text} not FAILED`);
        }
        if (t[i].role === "user" && t[i].text.startsWith("stress ") && !t[i].text.startsWith("stress seed") && !conv.imported) {
          if (owner.has(t[i].text) && owner.get(t[i].text) !== conv.id) { problems.push(`${t[i].text} in two conversations`); }
          owner.set(t[i].text, conv.id);
        }
      }
      if (!t.length) { continue; }
      if (conv.notSaved) {
        if (!injected.has(conv.id)) { problems.push(`${conv.id}: not saved without an injected refusal`); }
        continue;
      }
      const plain = plains.get(conv.id);
      const stored = plain && plain.conversation.messages.map((m) => ({ role: m.role, status: m.status || "", text: m.text }));
      if (!plain || JSON.stringify(stored) !== JSON.stringify(t)) {
        // The other page may hold an older copy of a shared record; the page that wrote it last must match.
        const other = views.some((v) => v !== view && v.some((x) => x.id === conv.id && JSON.stringify(x.thread) === JSON.stringify(stored)));
        if (!other) { problems.push(`${conv.id}: record does not match`); }
      }
    }
  }
  const ivs = [...recs.map(([, v]) => JSON.parse(v).cipher.iv), ...files.map((f) => JSON.parse(f).cipher.iv)];
  if (new Set(ivs).size !== ivs.length) { problems.push("an IV repeats"); }
  const held = new Set(views.flatMap((v) => v.filter((x) => x.thread.length).map((x) => x.id)));
  const savedHeld = new Set(views.flatMap((v) => v.filter((x) => x.thread.length && !x.notSaved).map((x) => x.id)));
  const keys = new Set(recs.map(([key]) => key));
  [...keys].forEach((key) => { if (!held.has(key)) { problems.push(`record ${key} not held`); } });
  [...savedHeld].forEach((id) => { if (!keys.has(id)) { problems.push(`conversation ${id} missing`); } });

  await p1.reload();
  await p2.reload();
  await unlocked(p1);
  await unlocked(p2);
  const listed = await p1.evaluate(() => Array.from(document.querySelectorAll("#chats .chat"))
    .filter((x) => !x.querySelector("em").textContent.endsWith(" 0 messages")).map((x) => x.dataset.id));
  if (JSON.stringify([...new Set(listed)].sort()) !== JSON.stringify([...keys].sort())) { problems.push("the reloaded list is not the record keys"); }
  check(`ui stress: 60 operations over 2 pages, ${recs.length} records, ${files.length} exports, ${injected.size} refused writes`,
    problems.length === 0, problems.slice(0, 8).join("; "));
  await context.close();
}
