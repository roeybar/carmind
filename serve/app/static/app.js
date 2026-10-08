/* carmind operator page. Every string, colour, language and limit comes from /api/config.
   Dictation and read-aloud use the device's own speech engines; conversations are kept only in
   this browser (IndexedDB, AES-256-GCM under a passphrase) or in a file the user saves. */
(function () {
  "use strict";

  /* ---------- 1. helpers ---------- */
  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }
  function clear(node) { node.replaceChildren(); }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function clock(ms) { var d = new Date(ms); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function dur(s) { return Math.floor((s || 0) / 60) + ":" + pad((s || 0) % 60); }
  function fmt(template, n) { return template.replace("{n}", String(n)); }
  // A lookup of an untrusted key that never reaches the prototype chain.
  function own(obj, key) { return obj && Object.prototype.hasOwnProperty.call(obj, key) ? obj[key] : undefined; }
  function capitalize(word) { return word ? word.charAt(0).toUpperCase() + word.slice(1) : ""; }
  var TIMEOUT = {};
  function timeout(ms) { return new Promise(function (resolve) { setTimeout(function () { resolve(TIMEOUT); }, ms); }); }
  function freshId() {
    var bytes = crypto.getRandomValues(new Uint8Array(8));
    return Array.prototype.map.call(bytes, function (b) { return (b < 16 ? "0" : "") + b.toString(16); }).join("");
  }
  // The caller number as the server reads it (Python's str.strip() whitespace, built from code points so
  // this file stays ASCII).
  var SPACES = "\\t\\n\\x0b\\x0c\\r\\x1c-\\x20\\x85\\xa0" + String.fromCharCode(0x1680, 0x2000) + "-" +
    String.fromCharCode(0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000);
  var EDGE_SPACE = new RegExp("^[" + SPACES + "]+|[" + SPACES + "]+$", "g");
  function phoneDigits(text) {
    var value = String(text || "");
    var trimmed = value.replace(EDGE_SPACE, "");
    if (value.length > 40 || !/^\+?[0-9 ().\-]*$/.test(trimmed)) { return ""; }
    return trimmed.replace(/[^0-9]/g, "");
  }

  /* ---------- 2. state ---------- */
  var S = {
    cfg: null, copy: null, lim: null, customers: {}, listLoaded: false, convs: new Map(), open: null,
    lang: "en-US", pending: new Map(), seq: 0, sent: false,
    detectSeq: 0, SR: null, micOn: false, needInstall: false, installing: false,
    rec: false, recSeq: 0, locked: false, pressing: false, pressAt: 0, pressY: 0, heard: "", interim: "", level: 0,
    speaking: null, voices: [],
    db: null, savesState: "starting", baseKey: null, keyring: new Map(), writeKey: null,
    offerForce: false, lockedCount: 0, damagedCount: 0, importing: null, savesBusy: false
  };
  var recog = null, session = null, analyser = null, buf = null, raf = 0, timer = 0, t0 = 0;
  var SECURE = window.isSecureContext === true;

  /* ---------- 3. boot ---------- */
  async function boot() {
    var cfg;
    try {
      var resp = await fetch("/api/config");
      if (!resp.ok) { throw new Error("config"); }
      cfg = await resp.json();
    } catch (err) {
      $("boot-failed").hidden = false;
      $("ask").disabled = true;
      return;
    }
    S.cfg = cfg; S.copy = cfg.copy; S.lim = cfg.limits;
    applyConfig();
    paintSaves();
    var first = newConversationObject();
    S.convs.set(first.id, first);
    openConversation(first);
    $("boot-failed").hidden = true;
    loadCustomers();
    if (!SECURE) {
      setNotice(S.copy.insecure_context);
      S.savesState = "insecure";
      paintSaves();
    } else {
      detect();
      startSaves();
    }
    if (window.speechSynthesis) {
      S.voices = speechSynthesis.getVoices();
      speechSynthesis.addEventListener("voiceschanged", function () { S.voices = speechSynthesis.getVoices(); });
    }
  }

  function applyConfig() {
    var c = S.copy, theme = S.cfg.theme, root = document.documentElement.style;
    root.setProperty("--primary", theme.primary);
    root.setProperty("--dark", theme.dark);
    root.setProperty("--record", theme.record);
    $("product").textContent = S.cfg.product_name;
    $("status").textContent = c.status;
    Array.prototype.forEach.call(document.querySelectorAll("[data-copy]"), function (node) {
      node.textContent = c[node.getAttribute("data-copy")];
    });
    $("menu").setAttribute("aria-label", c.menu_label);
    $("new").textContent = c.new_label;
    $("newDrawer").textContent = c.new_conversation;
    $("mic").setAttribute("aria-label", c.mic_label);
    $("core").setAttribute("aria-label", c.stop_label);
    $("stopwide").appendChild(document.createTextNode(c.stop_review));
    $("problem").setAttribute("placeholder", c.problem_placeholder);
    $("problem").setAttribute("maxlength", String(S.lim.problem));
    $("caller").setAttribute("placeholder", c.caller_placeholder);
    $("caller").setAttribute("maxlength", String(S.lim.caller));
    $("plate").setAttribute("placeholder", c.plate_placeholder);
    $("plate").setAttribute("maxlength", String(S.lim.plate));
    $("locked").textContent = c.inputs_locked;
    $("privacy").textContent = c.privacy;
    $("pass").setAttribute("placeholder", c.passphrase);
    $("pass2").setAttribute("placeholder", c.passphrase_repeat);
    $("import-pass").setAttribute("placeholder", c.passphrase);
    $("pass-warning").textContent = c.passphrase_warning;
    $("set-pass").textContent = c.set_pass;
    $("unlock").textContent = c.unlock;
    $("force").textContent = c.force;
    $("import").textContent = c.import;
    $("import-go").textContent = c.unlock;
    var lang = $("lang");
    clear(lang);
    S.cfg.languages.forEach(function (l) {
      var option = el("option", null, l.label);
      option.value = l.code;
      lang.appendChild(option);
    });
    S.lang = S.cfg.languages.length ? S.cfg.languages[0].code : "en-US";
    lang.value = S.lang;
    applyDir();
    var chips = document.querySelectorAll("button.example");
    S.cfg.examples.forEach(function (text, i) { if (chips[i]) { chips[i].textContent = text; } });
    paintHint();
  }

  function applyDir() {
    var meta = S.cfg.languages.filter(function (l) { return l.code === S.lang; })[0] || {};
    $("app").setAttribute("dir", meta.rtl ? "rtl" : "ltr");
  }

  // Global notices (speech, saves, the customer list) show when they happen; a conversation's own
  // notice (conv.notice) is painted when that conversation is open.
  function setNotice(text, action) {
    var box = $("error");
    clear(box);
    if (text) { box.appendChild(document.createTextNode(text)); }
    if (text && action) {
      var button = el("button", null, action.label);
      button.type = "button";
      button.addEventListener("click", action.run);
      box.appendChild(el("br"));
      box.appendChild(button);
    }
    box.hidden = !text;
  }

  function loadCustomers() {
    fetch("/api/customers").then(function (resp) { return resp.json(); }).then(function (data) {
      var select = $("customer");
      data.customers.forEach(function (c) {
        S.customers[c.id] = c;
        var option = el("option", null, c.name + " - " + c.phone);
        option.value = c.id;
        select.appendChild(option);
      });
      S.listLoaded = true;
      S.firstCustomer = data.customers.length ? data.customers[0] : null;
      var conv = S.open;
      if (S.firstCustomer && !S.sent && conv.caller.caller_number === "" && conv.caller.plate === "") {
        conv.caller.customer_id = S.firstCustomer.id;
        conv.caller.caller_number = S.firstCustomer.phone;
      }
      paintInputs(conv);
      paintCard(conv);
    }).catch(function () {
      setNotice(S.copy.customers_failed);
    });
  }

  /* ---------- conversations ---------- */
  function newConversationObject() {
    var now = Date.now(), first = S.listLoaded ? S.firstCustomer : null;
    return {
      id: freshId(), title: "", created_at: now, updated_at: now,
      caller: { customer_id: first ? first.id : "", caller_number: first ? first.phone : "", plate: "", after_hours: false },
      imported: false, messages: [], notice: "", stored: null, unsaved: true, saving: false, again: false
    };
  }

  function openConversation(conv) {
    S.open = conv;
    renderThread();
    paintInputs(conv);
    paintCard(conv);
    setNotice(conv.notice || "");
    paintAsk();
    renderChats();
  }

  function newConversation() {
    var conv = newConversationObject();
    S.convs.set(conv.id, conv);
    openConversation(conv);
    toggleDrawer(false);
  }

  function toggleDrawer(open) { $("scrim").dataset.open = open ? "1" : "0"; }

  function sortedConvs() {
    return Array.from(S.convs.values()).sort(function (a, b) {
      return b.created_at - a.created_at || (a.id < b.id ? -1 : 1);
    });
  }

  function renderChats() {
    var box = $("chats"), c = S.copy;
    clear(box);
    sortedConvs().forEach(function (conv) {
      var row = el("div", "chat");
      row.dataset.active = conv === S.open ? "1" : "0";
      row.dataset.id = conv.id;
      row.appendChild(el("i"));
      var body = el("div", "body");
      body.appendChild(el("b", null, conv.title));
      var n = conv.messages.length;
      body.appendChild(el("em", null, clock(conv.created_at) + " - " + (n === 1 ? c.count_one : fmt(c.count_many, n))));
      if (conv.imported) { body.appendChild(el("span", "tag imported", c.imported)); }
      if (n && conv.unsaved) { body.appendChild(el("span", "tag not-saved", c.not_saved)); }
      row.appendChild(body);
      var exp = el("button", "exp");
      exp.type = "button";
      exp.setAttribute("aria-label", c.save_file);
      exp.disabled = !SECURE;
      exp.addEventListener("click", function (ev) { ev.stopPropagation(); exportConversation(conv); });
      row.appendChild(exp);
      var del = el("button", "del");
      del.type = "button";
      del.setAttribute("aria-label", c.delete_label);
      del.disabled = !SECURE || conv === S.open || S.pending.has(conv);
      del.addEventListener("click", function (ev) { ev.stopPropagation(); removeConversation(conv); });
      row.appendChild(del);
      row.addEventListener("click", function () { openConversation(conv); toggleDrawer(false); });
      box.appendChild(row);
    });
  }

  /* ---------- 4. drawer inputs and the customer card ---------- */
  function renderCustomer(c, unknown) {
    var card = $("customer-card"), copy = S.copy;
    clear(card);
    if (!c) {
      if (unknown) { card.appendChild(el("div", "muted", copy.not_on_record)); }
      return;
    }
    var cars = (c.vehicles || []).map(function (v) { return v.plate + " - " + v.car; }).join("; ");
    [[copy.card_name, c.name], [copy.card_phone, c.phone], [copy.card_cars, cars],
     [copy.card_coverage, capitalize(c.coverage)], [copy.card_location, c.location]].forEach(function (row) {
      var line = el("div");
      line.appendChild(el("span", null, row[0]));
      line.appendChild(document.createTextNode(row[1]));
      card.appendChild(line);
    });
  }

  function typedMatch(callerNumber) {
    var typed = phoneDigits(callerNumber), match = null;
    Object.keys(S.customers).forEach(function (id) {
      if (typed && phoneDigits(S.customers[id].phone) === typed) { match = S.customers[id]; }
    });
    return match;
  }

  function lastAnswer(conv) {
    for (var i = conv.messages.length - 1; i >= 0; i--) {
      if (conv.messages[i].kind === "answer") { return conv.messages[i]; }
    }
    return null;
  }

  // After an answer the card is the server's reading; before one, the typed-match rule.
  function paintCard(conv) {
    var a = lastAnswer(conv);
    if (a) {
      var id = a.details.customer_id;
      if (typeof id === "string") { renderCustomer(S.listLoaded ? own(S.customers, id) || null : null, false); }
      else { renderCustomer(null, true); }
      return;
    }
    renderCustomer(typedMatch(conv.caller.caller_number), false);
  }

  function inputsLocked(conv) { return conv.messages.length > 0 || S.pending.has(conv); }

  function paintInputs(conv) {
    var select = $("customer");
    if (conv.caller.customer_id && own(S.customers, conv.caller.customer_id)) { select.value = conv.caller.customer_id; }
    else { select.selectedIndex = -1; }
    $("caller").value = conv.caller.caller_number;
    $("plate").value = conv.caller.plate;
    $("after-hours").checked = conv.caller.after_hours;
    var locked = inputsLocked(conv);
    ["customer", "caller", "plate", "after-hours"].forEach(function (id) { $(id).disabled = locked; });
    $("locked").hidden = !locked;
  }

  function paintAsk() {
    var busy = S.pending.has(S.open);
    $("ask").disabled = busy;
    $("ask").setAttribute("aria-label", busy ? S.copy.asking : S.copy.ask_label);
  }

  /* ---------- 5. thread ---------- */
  function renderThread() {
    var box = $("thread"), conv = S.open, c = S.copy, busy = S.pending.has(conv);
    clear(box);
    if (conv.imported) { box.appendChild(el("div", "notice imported-notice", c.imported_notice)); }
    if (!conv.messages.length && !busy) {
      var empty = el("div", "empty"), mark = el("div", "mark");
      mark.appendChild(el("span"));
      empty.appendChild(mark);
      empty.appendChild(el("h2", null, c.empty_title));
      empty.appendChild(el("p", null, c.empty_body));
      box.appendChild(empty);
    }
    conv.messages.forEach(function (m) {
      // One message that cannot be drawn must not stop the rest of the page (Ask included).
      try {
        box.appendChild(row(m));
      } catch (err) {
        box.appendChild(row({ id: m.id, role: "assistant", created_at: m.created_at, kind: "failed", status: "",
                              text: S.copy.failed, details: { error: S.copy.request_failed, customer_id: null } }));
      }
    });
    if (busy) {
      var typing = el("div", "typing");
      typing.appendChild(el("i")); typing.appendChild(el("i")); typing.appendChild(el("i"));
      box.appendChild(typing);
    }
    box.scrollTop = box.scrollHeight;
    $("mic").classList.toggle("bounce", !conv.messages.length && !S.rec && !busy);
  }

  function row(m) {
    var node = el("div", "row " + (m.role === "user" ? "user" : "assistant"));
    var bubble = el("div", "bubble");
    if (m.role === "user") {
      bubble.setAttribute("dir", "auto");
      bubble.textContent = m.text;
    } else {
      node.setAttribute("data-status", m.status || "");
      if (m.status) { bubble.appendChild(el("span", "badge", own(S.cfg.badges, m.status) || m.status)); }
      var reply = el("span", "reply", m.text);
      reply.setAttribute("dir", "auto");
      bubble.appendChild(reply);
    }
    node.appendChild(bubble);
    var meta = el("div", "meta");
    if (m.role !== "user") {
      var speak = el("button", "speak");
      speak.type = "button";
      speak.appendChild(el("span", "ico"));
      speak.appendChild(document.createTextNode(S.speaking === m.id ? S.copy.read_stop : S.copy.read_aloud));
      speak.addEventListener("click", function () { readAloud(m); });
      meta.appendChild(speak);
    }
    meta.appendChild(el("span", null, clock(m.created_at)));
    node.appendChild(meta);
    if (m.role !== "user" && m.kind === "answer") { node.appendChild(renderDetails(m.details)); }
    return node;
  }

  function section(cls, title) {
    var box = el("div", cls);
    if (title) { box.appendChild(el("b", null, title)); }
    return box;
  }

  function renderDetails(d) {
    var c = S.copy, more = el("details", "more");
    more.appendChild(el("summary", null, c.details));
    if (d.source) { more.appendChild(el("div", "d-source", c.source_prefix + d.source)); }
    more.appendChild(renderDispatch(d.dispatch, d.choice));
    more.appendChild(renderNote(d.note));
    more.appendChild(renderContext(d.context));
    more.appendChild(renderChoice(d.choice));
    more.appendChild(renderToolCalls(d.tool_calls));
    more.appendChild(el("div", "d-provider", c.provider_prefix + (d.provider || c.provider_none)));
    return more;
  }

  function renderDispatch(dispatch, choice) {
    var c = S.copy, box = section("d-dispatch", c.dispatch_title);
    if (!dispatch || dispatch.error) { box.appendChild(el("div", "muted", c.no_dispatch)); return box; }
    box.appendChild(el("div", null, own(S.cfg.actions, dispatch.action) || dispatch.action));
    if (dispatch.provider) { box.appendChild(el("div", null, c.provider_prefix + dispatch.provider)); }
    if (dispatch.destination) { box.appendChild(el("div", null, c.destination_prefix + dispatch.destination)); }
    var level = capitalize(dispatch.coverage);
    box.appendChild(el("div", null, dispatch.covered ? c.covered_prefix + level : c.not_covered_prefix + level + c.not_covered_suffix));
    var steps = el("ol");
    (dispatch.steps || []).forEach(function (step) { steps.appendChild(el("li", null, step)); });
    box.appendChild(steps);
    if (choice && choice.status === "picked") { box.appendChild(el("div", "muted", c.dispatch_card_note)); }
    return box;
  }

  function renderNote(note) {
    var box = section("d-note", S.copy.note_title);
    box.appendChild(el("div", note ? null : "muted", note || S.copy.no_note));
    return box;
  }

  function optionName(choice, id) {
    var found = (choice.options || []).filter(function (o) { return o.id === id; })[0];
    return found ? found.name : String(id);
  }

  function appendReasoning(box, item) {
    if (item.withheld) { box.appendChild(el("div", null, S.copy.reasoning_withheld)); }
    else if (item.reasoning) { box.appendChild(el("div", null, S.copy.reasoning_prefix + item.reasoning)); }
  }

  function renderChoice(choice) {
    var c = S.copy, box = section("d-choice", c.choice_title);
    if (!choice) { box.appendChild(el("div", "muted", c.choice_none)); return box; }
    var lines = { picked: c.choice_picked + optionName(choice, choice.picked), asked: c.choice_asked,
                  rejected: c.choice_rejected, failed: c.choice_failed };
    box.appendChild(el("div", null, own(lines, choice.status) || c.choice_failed));
    if (choice.status === "picked") { box.appendChild(el("div", "muted", c.choice_picked_dispatch)); }
    box.appendChild(el("div", null, c.options_prefix + (choice.options || []).map(function (o) { return o.name; }).join("; ")));
    appendReasoning(box, choice);
    return box;
  }

  function renderContext(context) {
    var c = S.copy, box = section("d-context", c.context_title);
    if (!context) { box.appendChild(el("div", "muted", c.context_none)); return box; }
    var lines = { chosen: c.context_chosen + (context.flags || []).join(", "), none: c.context_empty,
                  rejected: c.context_rejected, failed: c.context_failed };
    box.appendChild(el("div", null, own(lines, context.status) || c.context_failed));
    if (context.status === "chosen") { box.appendChild(el("div", "muted", c.context_tighten)); }
    box.appendChild(el("div", null, c.options_prefix + (context.options || []).join("; ")));
    appendReasoning(box, context);
    return box;
  }

  function renderToolCalls(calls) {
    var c = S.copy, box = section("d-tools", c.tools_title), list = el("ul");
    (calls || []).forEach(function (call) {
      var item = el("li");
      item.appendChild(el("code", null, call.name));
      item.appendChild(document.createTextNode(c.by_prefix + call.by + c.by_sep + call.summary));
      list.appendChild(item);
    });
    box.appendChild(list);
    return box;
  }

  /* ---------- 6. send ---------- */
  function snapshot(conv) {
    var c = conv.caller;
    return JSON.stringify([c.customer_id, c.caller_number, c.plate, c.after_hours]);
  }

  function orNull(value) { return value === undefined ? null : value; }
  function listOf(value, pick) { return Array.isArray(value) ? value.map(pick) : value; }

  // The answer's details, with only the fields the page shows (the same keys validate() keeps).
  function answerDetails(data) {
    var v = data.verdict || {}, d = data.dispatch, ch = data.choice, cx = data.context, dispatch = null;
    if (d && typeof d === "object") {
      dispatch = "error" in d ? { error: d.error } : { action: d.action, provider: orNull(d.provider),
        destination: orNull(d.destination), coverage: d.coverage, covered: d.covered, steps: d.steps };
    }
    return {
      source: orNull(v.source), note: data.note === undefined || data.note === null ? "" : data.note,
      provider: orNull(data.provider), error: orNull(data.error),
      customer_id: data.customer && typeof data.customer.id === "string" && !data.customer.error ? data.customer.id : null,
      dispatch: dispatch,
      choice: ch && typeof ch === "object" ? { status: ch.status, picked: orNull(ch.picked),
        options: listOf(ch.options, function (o) { return { id: o && o.id, name: o && o.name }; }),
        reasoning: orNull(ch.reasoning), withheld: ch.withheld } : null,
      context: cx && typeof cx === "object" ? { status: cx.status, flags: cx.flags, options: cx.options,
        reasoning: orNull(cx.reasoning), withheld: cx.withheld } : null,
      tool_calls: listOf(data.tool_calls || [], function (t) { return { name: t && t.name, by: t && t.by, summary: t && t.summary }; })
    };
  }

  async function send() {
    var conv = S.open, c = S.copy, lim = S.lim;
    if (S.pending.has(conv)) { return; }
    if (conv.messages.length > lim.messages - 2) {
      conv.notice = c.conversation_full;
      setNotice(conv.notice);
      return;
    }
    var problem = $("problem").value.slice(0, lim.problem).trim();
    if (!problem) {
      conv.notice = c.problem_first;
      setNotice(conv.notice);
      return;
    }
    var caller = conv.caller, mine = ++S.seq, asked = snapshot(conv), now = Date.now();
    var body = { caller_number: caller.caller_number, plate: caller.plate.trim(), problem: problem,
                 after_hours: caller.after_hours };
    conv.messages.push({ id: freshId(), role: "user", created_at: now, text: problem });
    if (!conv.title) { conv.title = problem.slice(0, 60); }
    conv.updated_at = now;
    conv.unsaved = true;
    conv.notice = "";
    $("problem").value = "";
    $("problem").style.height = "auto";
    S.pending.set(conv, { seq: mine, snapshot: asked });
    S.sent = true;
    if (conv === S.open) { renderThread(); setNotice(""); paintInputs(conv); }
    paintAsk();
    renderChats();
    var out = null;
    try {
      var resp = await fetch("/api/agent", { method: "POST", headers: { "Content-Type": "application/json" },
                                             body: JSON.stringify(body) });
      var data = null;
      try { data = await resp.json(); } catch (err) { data = null; }
      out = { ok: resp.ok, status: resp.status, data: data };
    } catch (err) {
      out = null;
    }
    var p = S.pending.get(conv);
    if (!p || p.seq !== mine) { return; }
    S.pending.delete(conv);
    var S_MAX = lim.detail_text, msg = { id: freshId(), role: "assistant", created_at: Date.now() };
    var failed = function (error) {
      msg.kind = "failed"; msg.status = ""; msg.text = c.failed;
      msg.details = { error: String(error).slice(0, S_MAX), customer_id: null };
    };
    if (snapshot(conv) !== p.snapshot) {
      failed(c.request_failed);
      conv.notice = c.request_failed;
    } else if (out === null) {
      failed(c.network_error);
      conv.notice = c.network_error;
    } else if (out.status >= 500 || !out.data) {
      var e5 = out.data && typeof out.data.error === "string" ? out.data.error : c.request_failed;
      failed(e5);
      conv.notice = e5;
    } else if (!out.ok) {
      var e = typeof out.data.error === "string" ? out.data.error : c.request_failed;
      var pr = typeof out.data.precaution === "string" && out.data.precaution ? out.data.precaution : "";
      msg.kind = "rejected"; msg.status = "";
      msg.text = (pr ? e + " " + pr : e).slice(0, lim.assistant_text);
      msg.details = { error: e.slice(0, S_MAX), customer_id: null };
      conv.notice = pr ? e + " " + pr : e;
    } else if (typeof out.data.reply !== "string" || !out.data.verdict || STATUSES.indexOf(out.data.verdict.status) < 0) {
      failed(c.request_failed);  // a 200 without the answer's shape
      conv.notice = c.request_failed;
    } else if (out.data.error === "internal error") {
      failed(c.request_failed);  // the service's last-resort reply
      conv.notice = c.request_failed;
    } else {
      var d = out.data;
      msg.kind = "answer";
      msg.status = d.verdict.status;
      msg.text = d.reply;
      msg.details = answerDetails(d);
      conv.notice = d.error ? c.model_error_prefix + d.error : "";
    }
    // The answer gate: shape, types and enums only (the caps apply at save and export).
    try {
      vMessage(msg, { caps: false });
    } catch (err) {
      if (err !== BAD) { throw err; }
      failed(c.request_failed);
      conv.notice = c.request_failed;
    }
    conv.messages.push(msg);
    conv.updated_at = msg.created_at;
    try {
      if (conv === S.open) {
        renderThread();
        setNotice(conv.notice);
        paintInputs(conv);
        paintCard(conv);
      }
    } finally {
      paintAsk();
      queueSave(conv);
    }
  }

  /* ---------- 7. speech (on-device only) ---------- */
  function setMic(on) {
    S.micOn = on;
    $("mic").disabled = !on;
    paintHint();
  }

  // The constructor that offers on-device recognition, preferring the unprefixed one.
  function localRecognizer() {
    var found = null;
    [window.SpeechRecognition, window.webkitSpeechRecognition].forEach(function (C) {
      if (!found && C && typeof C.available === "function" && C.prototype && "processLocally" in C.prototype) { found = C; }
    });
    return found;
  }

  async function detect() {
    var mine = ++S.detectSeq, c = S.copy;
    setMic(false);
    S.needInstall = false;
    if (!(window.SpeechRecognition || window.webkitSpeechRecognition)) { setNotice(c.dictation_none); return; }
    var SR = localRecognizer();
    if (!SR) { setNotice(c.dictation_server_only); return; }
    var result;
    try {
      result = await Promise.race([
        Promise.resolve().then(function () { return SR.available({ langs: [S.lang], processLocally: true }); }),
        timeout(3000)]);
    } catch (err) {
      result = TIMEOUT;
    }
    if (mine !== S.detectSeq) { return; }
    if (result === "unavailable") { setNotice(c.dictation_server_only); return; }
    if (result === "available" || result === "downloadable" || result === "downloading") {
      S.SR = SR;
      S.needInstall = result !== "available";
      if (result === "available" && $("error").textContent === c.dictation_installing) { setNotice(""); }
      setMic(true);
      return;
    }
    setNotice(c.dictation_local_failed);
  }

  async function install() {
    var mine = ++S.detectSeq, c = S.copy, ok;
    S.installing = true;
    setNotice(c.dictation_installing);
    try {
      ok = await S.SR.install({ langs: [S.lang], processLocally: true });
    } catch (err) {
      ok = false;
    }
    S.installing = false;
    if (mine !== S.detectSeq) { return; }
    if (ok === true) { detect(); return; }
    setMic(false);
    setNotice(c.dictation_local_failed);
  }

  function startRecognition() {
    var c = S.copy, r;
    try {
      r = new S.SR();
      r.processLocally = true;
    } catch (err) {
      setMic(false); setNotice(c.dictation_local_failed); return false;
    }
    if (r.processLocally !== true) { setMic(false); setNotice(c.dictation_server_only); return false; }
    r.lang = S.lang; r.continuous = true; r.interimResults = true;
    r.onresult = function (ev) {
      var fin = "", interim = "";
      for (var i = ev.resultIndex; i < ev.results.length; i++) {
        var t = ev.results[i][0].transcript;
        if (ev.results[i].isFinal) { fin += t; } else { interim += t; }
      }
      if (fin) { S.heard = (S.heard + " " + fin.trim()).trim(); S.interim = ""; } else { S.interim = interim; }
      paintRec();
    };
    r.onerror = function (ev) {
      if (ev.error === "not-allowed") {
        stop(); setMic(false); setNotice(c.dictation_denied);
      } else if (ev.error === "service-not-allowed") {
        stop(); setMic(false); setNotice(c.dictation_local_failed);
      } else {
        stop(); setNotice(c.dictation_stopped);
      }
    };
    // The engine ended the session on its own: keep the text heard so far.
    r.onend = function () {
      if (recog === r && S.rec) { stop(); setNotice(c.dictation_stopped); }
    };
    try {
      r.start();
    } catch (err) {
      setMic(false); setNotice(c.dictation_local_failed); return false;
    }
    recog = r;
    return true;
  }

  async function pressStart(e) {
    if (S.rec) { stop(); return; }
    if (!S.micOn) { return; }
    if (S.needInstall) { if (!S.installing) { install(); } return; }
    if (e && e.currentTarget && e.currentTarget.setPointerCapture) {
      try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) { /* not capturable */ }
    }
    S.pressY = e ? e.clientY : 0; S.pressAt = Date.now(); t0 = Date.now();
    setNotice("");
    if (!startRecognition()) { return; }
    S.rec = true; S.locked = false; S.pressing = true; S.heard = ""; S.interim = ""; S.level = 0;
    paintRec();
    paintLede();
    timer = setInterval(paintLede, 250);
    // This press's session: stop() closes exactly its stream and audio context.
    var mine = { seq: ++S.recSeq, stream: null, ac: null }, got = null;
    session = mine;
    try {
      got = navigator.mediaDevices ? await navigator.mediaDevices.getUserMedia({ audio: true }) : null;
    } catch (err) {
      got = null;
    }
    if (mine.seq !== S.recSeq || !S.rec) {
      if (got) { got.getTracks().forEach(function (t) { t.stop(); }); }
      return;
    }
    mine.stream = got;
    if (got) { meter(mine); }
  }

  function paintLede() {
    var s = Math.round((Date.now() - t0) / 1000);
    $("lede").textContent = S.copy.listening_lede + " - " + dur(s) + " - " + S.lang.toUpperCase();
  }

  // The level rings only: the stream feeds an AnalyserNode and is never recorded.
  function meter(sess) {
    try {
      var ac = sess.ac = new (window.AudioContext || window.webkitAudioContext)();
      analyser = ac.createAnalyser(); analyser.fftSize = 512;
      ac.createMediaStreamSource(sess.stream).connect(analyser);
      buf = new Uint8Array(analyser.fftSize);
      var tick = function () {
        if (!analyser) { return; }
        analyser.getByteTimeDomainData(buf);
        var sum = 0;
        for (var i = 0; i < buf.length; i++) { var v = (buf[i] - 128) / 128; sum += v * v; }
        S.level = Math.min(1, Math.sqrt(sum / buf.length) * 5.2);
        paintBlast();
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    } catch (err) {
      analyser = null;
    }
  }

  function pressMove(e) {
    if (!S.pressing || S.locked) { return; }
    var dy = Math.min(0, e.clientY - S.pressY);
    if (dy <= -58) { S.locked = true; S.pressing = false; paintRec(); return; }
    $("lockcue").style.transform = "translateY(" + Math.max(-10, dy * 0.2) + "px)";
    $("mic").style.transform = "translateY(" + Math.max(-14, dy * 0.28) + "px)";
  }

  function pressEnd(force) {
    if (!S.rec) { S.pressing = false; paintRec(); return; }
    if (S.locked && !force) { return; }
    if (!force && Date.now() - S.pressAt < 300) { S.locked = true; S.pressing = false; paintRec(); return; }
    stop();
  }

  // Ends dictation; the text heard so far goes into the problem box and is never sent.
  function stop() {
    var text = (S.heard + " " + S.interim).trim();
    if (raf) { cancelAnimationFrame(raf); }
    raf = 0; analyser = null;
    var sess = session;
    session = null;
    if (sess && sess.ac) { try { sess.ac.close(); } catch (err) { /* already closed */ } }
    if (timer) { clearInterval(timer); }
    timer = 0;
    if (recog) {
      var r = recog;
      recog = null;
      r.onresult = null; r.onerror = null; r.onend = null;
      try { r.stop(); } catch (err) { /* already stopped */ }
    }
    if (sess && sess.stream) { sess.stream.getTracks().forEach(function (t) { t.stop(); }); }
    S.rec = false; S.locked = false; S.pressing = false; S.level = 0; S.heard = ""; S.interim = "";
    if (text) {
      var box = $("problem");
      box.value = (box.value + " " + text).trim().slice(0, S.lim.problem);
      box.focus();
    }
    paintRec();
  }

  function paintRec() {
    var c = S.copy;
    $("overlay").dataset.open = S.rec ? "1" : "0";
    $("stopwide").hidden = !S.locked;
    $("mic").dataset.live = S.rec ? "1" : "0";
    $("mic").replaceChildren(el("span", S.locked ? "square" : "stem"));
    $("core").replaceChildren(el("span", S.locked ? "square" : "stem"));
    $("lockcue").style.opacity = S.pressing ? "1" : "0";
    if (!S.pressing) { $("lockcue").style.transform = "translateY(8px)"; $("mic").style.transform = "none"; }
    var heard = $("heardBox");
    clear(heard);
    if (S.heard || S.interim) {
      heard.appendChild(document.createTextNode(S.heard + " "));
      heard.appendChild(el("em", null, S.interim));
    } else {
      heard.appendChild(el("em", null, c.listening));
    }
    $("overlayFoot").textContent = S.locked ? c.locked_hint : c.release_hint;
    paintBlast(); paintHint();
    if (!S.rec) { renderThread(); }
  }

  function paintBlast() {
    var L = S.level;
    $("core").style.transform = "scale(" + (1 + L * 0.16).toFixed(3) + ")";
    $("ring1").style.transform = "scale(" + (1.18 + L * 0.75).toFixed(3) + ")";
    $("ring2").style.transform = "scale(" + (1.5 + L * 1.35).toFixed(3) + ")";
    $("ring3").style.transform = "scale(" + (1.85 + L * 2.05).toFixed(3) + ")";
    $("ring1").style.opacity = (0.55 + L * 0.35).toFixed(2);
    $("ring2").style.opacity = (0.4 + L * 0.3).toFixed(2);
    $("ring3").style.opacity = (0.16 + L * 0.3).toFixed(2);
  }

  function paintHint() {
    var c = S.copy;
    if (!c) { return; }
    $("hint").textContent = !S.micOn ? "" : S.locked ? c.locked_hint : (S.rec ? c.recording_hint : c.idle_hint);
  }

  /* ---------- 8. read-aloud (on-device voices only) ---------- */
  function localVoice() {
    var prefix = S.cfg.reply_language.slice(0, 2);
    return S.voices.filter(function (v) { return v.localService && v.lang && v.lang.slice(0, 2) === prefix; })[0] || null;
  }

  function readAloud(m) {
    if (!window.speechSynthesis) { setNotice(S.copy.read_aloud_none); return; }
    if (S.speaking === m.id) { speechSynthesis.cancel(); S.speaking = null; renderThread(); return; }
    var voice = localVoice();
    if (!voice) { setNotice(S.copy.read_aloud_none); return; }
    speechSynthesis.cancel();
    var u = new SpeechSynthesisUtterance(m.text);
    u.lang = S.cfg.reply_language;
    u.voice = voice;
    u.onend = function () { if (S.speaking === m.id) { S.speaking = null; renderThread(); } };
    speechSynthesis.speak(u);
    S.speaking = m.id;
    renderThread();
  }

  /* ---------- 9. saves, export and import ---------- */
  var FORMAT = "carmind-conversation", DATA_FORMAT = "carmind-conversation-data", KDF = "PBKDF2-SHA256";
  var CIPHER = "AES-256-GCM", ITERATIONS = 600000, MAX_ITERATIONS = 2000000;
  var STATUSES = ["not_drivable", "drivable_caution", "drivable", "unclear", "unsupported"];
  var HEX16 = /^[0-9a-f]{16}$/;
  var utf8 = new TextEncoder(), unutf8 = new TextDecoder();

  function b64(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) { s += String.fromCharCode(bytes[i]); }
    return btoa(s);
  }
  function unb64(text) {
    if (typeof text !== "string" || text.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(text)) { return null; }
    var s = atob(text), out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) { out[i] = s.charCodeAt(i); }
    return out;
  }
  function isObj(x) { return x !== null && typeof x === "object" && !Array.isArray(x); }

  // The envelope's shape, checked before any key derivation: the decoded salt, IV and ciphertext, or null.
  function isEnvelope(env) {
    if (!isObj(env) || env.format !== FORMAT || env.v !== 1 || !isObj(env.kdf) || !isObj(env.cipher)) { return null; }
    if (env.kdf.name !== KDF || env.cipher.name !== CIPHER) { return null; }
    var it = env.kdf.iterations;
    if (!Number.isInteger(it) || it < ITERATIONS || it > MAX_ITERATIONS) { return null; }
    var salt = unb64(env.kdf.salt), iv = unb64(env.cipher.iv), ct = unb64(env.ct);
    if (!salt || salt.length !== 16 || !iv || iv.length !== 12 || !ct || ct.length < 16) { return null; }
    return { salt: salt, iv: iv, ct: ct, iterations: it };
  }

  function aad(env) {
    return utf8.encode(JSON.stringify([env.format, env.v, env.kdf.name, env.kdf.iterations, env.kdf.salt,
                                       env.cipher.name, env.cipher.iv]));
  }

  // The passphrase string goes straight into a non-extractable PBKDF2 key; the fields are emptied.
  async function baseKeyFrom(ids) {
    var text = $(ids[0]).value;
    try {
      return await crypto.subtle.importKey("raw", utf8.encode(text), "PBKDF2", false, ["deriveKey"]);
    } finally {
      ids.forEach(function (id) { $(id).value = ""; });
    }
  }

  // One derived AES key per (base key, salt, iterations); `cache` serves a base key that is not the session's yet.
  async function keyFor(base, salt, iterations, cache) {
    var ring = cache || (base === S.baseKey ? S.keyring : null), name = b64(salt) + ":" + iterations;
    if (ring && ring.has(name)) { return ring.get(name); }
    var key = await crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt: salt, iterations: iterations },
                                            base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    if (ring) { ring.set(name, key); }
    return key;
  }

  async function seal(plain, writeKey) {
    var iv = crypto.getRandomValues(new Uint8Array(12));
    var env = { format: FORMAT, v: 1, kdf: { name: KDF, iterations: writeKey.iterations, salt: b64(writeKey.salt) },
                cipher: { name: CIPHER, iv: b64(iv) }, ct: "" };
    var ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: iv, additionalData: aad(env) }, writeKey.key,
                                         utf8.encode(JSON.stringify(plain)));
    env.ct = b64(new Uint8Array(ct));
    return JSON.stringify(env);
  }

  // Every failure (wrong passphrase, a changed byte, bad JSON inside) is the same error.
  async function openEnvelope(env, parts, base, cache) {
    try {
      var key = await keyFor(base, parts.salt, parts.iterations, cache);
      var plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: parts.iv, additionalData: aad(env) }, key, parts.ct);
      return JSON.parse(unutf8.decode(plain));
    } catch (err) {
      throw new Error("WrongPassphraseOrCorrupt");
    }
  }

  /* validate(): rebuilds a conversation from the known fields only; any other type rejects it all. */
  var BAD = {};
  // The length and count caps; vMessage(m, {caps: false}) checks shape, types and enums only.
  var capsOn = true;
  function cap(n) { return capsOn ? n : Infinity; }
  function need(ok) { if (!ok) { throw BAD; } }
  function str(x, max) { return typeof x === "string" && x.length <= cap(max); }
  function nstr(x, max) { return x === null || str(x, max); }
  function strList(x, maxItems, maxLen) {
    need(Array.isArray(x) && x.length <= cap(maxItems) && x.every(function (s) { return str(s, maxLen); }));
    return x.slice();
  }
  function time(x) { need(Number.isSafeInteger(x) && x >= 0); return x; }

  function vDispatch(d, S_MAX) {
    if (d === null) { return null; }
    need(isObj(d));
    if ("error" in d) { need(str(d.error, S_MAX)); return { error: d.error }; }
    need(str(d.action, 64) && nstr(d.provider, S_MAX) && nstr(d.destination, S_MAX) && str(d.coverage, 64) &&
         typeof d.covered === "boolean");
    return { action: d.action, provider: d.provider, destination: d.destination, coverage: d.coverage,
             covered: d.covered, steps: strList(d.steps, 64, S_MAX) };
  }
  function vChoice(ch, S_MAX) {
    if (ch === null) { return null; }
    need(isObj(ch) && ["picked", "asked", "rejected", "failed"].indexOf(ch.status) >= 0 && nstr(ch.picked, 200) &&
         Array.isArray(ch.options) && ch.options.length <= cap(20) && nstr(ch.reasoning, S_MAX) &&
         typeof ch.withheld === "boolean");
    return { status: ch.status, picked: ch.picked, options: ch.options.map(function (o) {
      need(isObj(o) && str(o.id, 200) && str(o.name, S_MAX));
      return { id: o.id, name: o.name };
    }), reasoning: ch.reasoning, withheld: ch.withheld };
  }
  function vContext(cx, S_MAX) {
    if (cx === null) { return null; }
    need(isObj(cx) && ["chosen", "none", "rejected", "failed"].indexOf(cx.status) >= 0 && nstr(cx.reasoning, S_MAX) &&
         typeof cx.withheld === "boolean");
    return { status: cx.status, flags: strList(cx.flags, 16, 64), options: strList(cx.options, 16, 64),
             reasoning: cx.reasoning, withheld: cx.withheld };
  }
  function vMessage(m, opts) {
    if (opts && opts.caps === false) {
      capsOn = false;
      try { return vMessage(m); } finally { capsOn = true; }
    }
    var lim = S.lim, S_MAX = lim.detail_text;
    need(isObj(m) && str(m.id, 16) && HEX16.test(m.id) && (m.role === "user" || m.role === "assistant"));
    var out = { id: m.id, role: m.role, created_at: time(m.created_at) };
    if (m.role === "user") { need(str(m.text, lim.problem)); out.text = m.text; return out; }
    var d = m.details;
    need(isObj(d));
    if (m.kind === "answer") {
      need(STATUSES.indexOf(m.status) >= 0 && str(m.text, lim.assistant_text));
      need(nstr(d.source, S_MAX) && str(d.note, S_MAX) && nstr(d.provider, 64) && nstr(d.error, S_MAX) &&
           nstr(d.customer_id, 40) && "dispatch" in d && "choice" in d && "context" in d &&
           Array.isArray(d.tool_calls) && d.tool_calls.length <= cap(lim.tool_calls));
      out.kind = "answer"; out.status = m.status; out.text = m.text;
      out.details = { source: d.source, note: d.note, provider: d.provider, error: d.error, customer_id: d.customer_id,
        dispatch: vDispatch(d.dispatch, S_MAX), choice: vChoice(d.choice, S_MAX), context: vContext(d.context, S_MAX),
        tool_calls: d.tool_calls.map(function (t) {
          need(isObj(t) && str(t.name, 64) && (t.by === "model" || t.by === "guard") && str(t.summary, 200));
          return { name: t.name, by: t.by, summary: t.summary };
        }) };
      return out;
    }
    need((m.kind === "failed" || m.kind === "rejected") && m.status === "" && str(d.error, S_MAX) &&
         d.customer_id === null);
    need(m.kind === "failed" ? m.text === S.copy.failed : str(m.text, lim.assistant_text));
    out.kind = m.kind; out.status = ""; out.text = m.text;
    out.details = { error: d.error, customer_id: null };
    return out;
  }
  function validate(plain) {
    try {
      need(isObj(plain) && plain.format === DATA_FORMAT && plain.v === 1 && isObj(plain.conversation));
      var c = plain.conversation, caller = c.caller;
      need(str(c.id, 16) && HEX16.test(c.id) && str(c.title, 60) && isObj(caller) && typeof c.imported === "boolean");
      need(str(caller.customer_id, 40) && str(caller.caller_number, 40) && str(caller.plate, 40) &&
           typeof caller.after_hours === "boolean");
      need(Array.isArray(c.messages) && c.messages.length <= S.lim.messages);
      return { id: c.id, title: c.title, created_at: time(c.created_at), updated_at: time(c.updated_at),
               caller: { customer_id: caller.customer_id, caller_number: caller.caller_number, plate: caller.plate,
                         after_hours: caller.after_hours },
               imported: c.imported, messages: c.messages.map(function (m) { return vMessage(m); }) };
    } catch (err) {
      if (err === BAD) { return null; }
      throw err;
    }
  }

  function plaintext(conv) {
    return { format: DATA_FORMAT, v: 1, conversation: {
      id: conv.id, title: conv.title, created_at: conv.created_at, updated_at: conv.updated_at,
      caller: { customer_id: conv.caller.customer_id, caller_number: conv.caller.caller_number, plate: conv.caller.plate,
                after_hours: conv.caller.after_hours },
      imported: conv.imported, messages: conv.messages } };
  }

  function fromClean(clean) {
    clean.notice = ""; clean.stored = null; clean.unsaved = false; clean.saving = false; clean.again = false;
    return clean;
  }

  /* IndexedDB: database "carmind", store "conversations", out-of-line keys, the envelope JSON text as the value. */
  function openStore(ms) {
    return new Promise(function (resolve) {
      var done = false;
      var finish = function (db) { if (!done) { done = true; resolve(db); } else if (db) { db.close(); } };
      setTimeout(function () { finish(null); }, ms);
      try {
        if (!window.indexedDB) { finish(null); return; }
        var req = indexedDB.open("carmind", 1);
        req.onupgradeneeded = function () { req.result.createObjectStore("conversations"); };
        req.onsuccess = function () { finish(req.result); };
        req.onerror = function () { finish(null); };
        req.onblocked = function () { finish(null); };
      } catch (err) {
        finish(null);
      }
    });
  }

  function readAll() {
    return new Promise(function (resolve) {
      try {
        var tx = S.db.transaction("conversations", "readonly"), store = tx.objectStore("conversations");
        var keys = store.getAllKeys(), values = store.getAll();
        tx.oncomplete = function () {
          resolve(keys.result.map(function (k, i) { return [k, values.result[i]]; })
            .sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0; }));
        };
        tx.onerror = function () { resolve([]); };
      } catch (err) {
        resolve([]);
      }
    });
  }

  // Compare-and-set in one readwrite transaction: "ok", "conflict" (the record is not `want`) or "full".
  function cas(id, want, next) {
    return new Promise(function (resolve) {
      var result = "full", tx;
      try { tx = S.db.transaction("conversations", "readwrite"); } catch (err) { resolve("full"); return; }
      var store = tx.objectStore("conversations"), got = store.get(id);
      got.onsuccess = function () {
        var current = got.result === undefined ? null : got.result;
        if (current !== want) { result = "conflict"; return; }
        try {
          if (next === null) { store.delete(id); } else { store.put(next, id); }
          result = "ok";
        } catch (err) {
          result = "full";
          try { tx.abort(); } catch (e2) { /* already finished */ }
        }
      };
      tx.oncomplete = function () { resolve(result); };
      tx.onabort = function () { resolve("full"); };
    });
  }

  async function startSaves() {
    S.db = await openStore(3000);
    if (!S.db) { S.savesState = "unavailable"; paintSaves(); renderChats(); return; }
    var records = await readAll();
    S.savesState = records.length ? "locked" : "set";
    paintSaves();
  }

  function show(id, on) { $(id).hidden = !on; }

  function paintSaves(message) {
    var c = S.copy, st = S.savesState, insecure = st === "insecure";
    var setting = st === "set" || st === "unavailable", locked = st === "locked";
    var state = { insecure: c.insecure_context, set: c.saves_set_intro, unavailable: c.saves_unavailable,
                  locked: S.offerForce ? c.unlock_wrong + " " + c.force_note : c.unlock_intro, open: c.saved_state,
                  starting: "" }[st];
    $("saves-state").textContent = message || state;
    show("pass", !insecure && !S.writeKey && (setting || locked));
    show("pass2", !insecure && !S.writeKey && (setting || (locked && S.offerForce)));
    show("pass-warning", !insecure && !S.writeKey && (setting || (locked && S.offerForce)));
    show("set-pass", !insecure && !S.writeKey && setting);
    show("unlock", !insecure && locked);
    show("force", !insecure && locked && S.offerForce);
    show("locked-count", S.lockedCount > 0);
    $("locked-count").textContent = fmt(c.locked_count, S.lockedCount);
    show("damaged-count", S.damagedCount > 0);
    $("damaged-count").textContent = fmt(c.damaged_count, S.damagedCount);
    $("import").disabled = insecure;
    show("import-row", !insecure && S.importing !== null);
  }

  async function setPassphrase() {
    var c = S.copy, a = $("pass").value, b = $("pass2").value;
    if (a.length < S.lim.passphrase_min) { paintSaves(c.passphrase_short); return; }
    if (a !== b) { paintSaves(c.passphrase_mismatch); return; }
    var base = await baseKeyFrom(["pass", "pass2"]);
    await startWriting(base, crypto.getRandomValues(new Uint8Array(16)));
    if (S.savesState !== "unavailable") { S.savesState = "open"; }
    paintSaves();
    S.convs.forEach(function (conv) { if (conv.messages.length) { conv.stored = null; queueSave(conv); } });
  }

  async function startWriting(base, salt) {
    S.baseKey = base;
    S.keyring = new Map();
    S.writeKey = { key: await keyFor(base, salt, ITERATIONS), salt: salt, iterations: ITERATIONS };
  }

  // One pass over the stored records under a base key, deriving once per salt and iterations: a record
  // that is not an envelope, or that opens but fails validate, is damaged; one that does not open is locked.
  async function openRecords(base) {
    var records = await readAll(), cache = new Map(), opened = [], locked = 0, damaged = 0, firstSalt = null;
    for (var i = 0; i < records.length; i++) {
      var key = records[i][0], text = records[i][1], env = null, parts = null;
      try { env = JSON.parse(text); } catch (err) { env = null; }
      parts = isEnvelope(env);
      if (!parts) { damaged++; continue; }
      var plain;
      try { plain = await openEnvelope(env, parts, base, cache); } catch (err) { locked++; continue; }
      var clean = validate(plain);
      if (!clean) { damaged++; continue; }
      clean.id = key;
      fromClean(clean).stored = text;
      opened.push(clean);
      if (!firstSalt) { firstSalt = parts.salt; }
    }
    return { opened: opened, locked: locked, damaged: damaged, firstSalt: firstSalt, cache: cache };
  }

  // Start writing under `base` and `salt`, keeping the keys the record pass derived.
  async function adopt(base, pass, salt) {
    S.offerForce = false;
    S.baseKey = base;
    S.keyring = pass.cache;
    S.writeKey = { key: await keyFor(base, salt, ITERATIONS), salt: salt, iterations: ITERATIONS };
    S.lockedCount = pass.locked; S.damagedCount = pass.damaged;
    pass.opened.forEach(function (conv) { S.convs.set(conv.id, conv); });
    S.savesState = "open";
    paintSaves();
    renderChats();
    S.convs.forEach(function (conv) { if (conv.messages.length && conv.stored === null) { queueSave(conv); } });
  }

  // Unlock and Force share one busy flag: while either is in flight the other (or itself) returns at once,
  // and the Unlock, Force and Set buttons are disabled.
  function exclusive(job) {
    return async function () {
      if (S.savesBusy) { return; }
      S.savesBusy = true;
      ["unlock", "force", "set-pass"].forEach(function (id) { $(id).disabled = true; });
      try {
        await job();
      } finally {
        S.savesBusy = false;
        ["unlock", "force", "set-pass"].forEach(function (id) { $(id).disabled = false; });
      }
    };
  }

  var unlock = exclusive(async function () {
    var c = S.copy;
    if (S.baseKey) { return; }  // one passphrase per session
    var base = await baseKeyFrom(["pass", "pass2"]);
    var pass = await openRecords(base);
    if (!pass.opened.length) {
      S.offerForce = true;
      paintSaves(c.unlock_wrong + " " + c.force_note);
      return;
    }
    await adopt(base, pass, pass.firstSalt);
  });

  // New saves under a passphrase typed twice, as for Set; the records it cannot open stay as they are.
  var force = exclusive(async function () {
    var c = S.copy, a = $("pass").value, b = $("pass2").value;
    if (S.baseKey) { return; }
    if (a.length < S.lim.passphrase_min) { paintSaves(c.passphrase_short); return; }
    if (a !== b) { paintSaves(c.passphrase_mismatch); return; }
    var base = await baseKeyFrom(["pass", "pass2"]);
    var pass = await openRecords(base);
    await adopt(base, pass, pass.opened.length ? pass.firstSalt : crypto.getRandomValues(new Uint8Array(16)));
  });

  // Writes are queued per conversation; the newest state wins.
  function queueSave(conv) {
    conv.unsaved = true;
    if (!S.writeKey || !S.db || S.savesState !== "open") { renderChats(); return; }
    if (conv.saving) { conv.again = true; return; }
    conv.saving = true;
    conv.saved = (async function () {
      try {
        do {
          conv.again = false;
          await saveOnce(conv);
        } while (conv.again);
      } finally {
        conv.saving = false;
        renderChats();
      }
    })();
  }

  async function saveOnce(conv) {
    if (!conv.messages.length || !S.convs.has(conv.id)) { return; }
    var plain = plaintext(conv);
    if (!validate(plain)) { conv.unsaved = true; setNotice(S.copy.save_invalid); return; }
    var text = await seal(plain, S.writeKey), result = await cas(conv.id, conv.stored, text);
    if (result === "conflict") {
      var newId = freshId();
      S.convs.delete(conv.id); conv.id = newId; S.convs.set(newId, conv);
      conv.stored = null;
      plain = plaintext(conv);
      text = await seal(plain, S.writeKey);
      result = await cas(newId, null, text);
      if (result === "ok") { setNotice(S.copy.save_conflict); }
    }
    if (result === "ok") {
      conv.stored = text;
      conv.unsaved = conv.again;
      return;
    }
    conv.unsaved = true;
    setNotice(S.copy.save_refused, { label: S.copy.save_file, run: function () { exportConversation(conv); } });
  }

  window.addEventListener("beforeunload", function (ev) {
    var unsaved = Array.from(S.convs.values()).some(function (conv) { return conv.messages.length && conv.unsaved; });
    if (unsaved) { ev.preventDefault(); ev.returnValue = ""; }
  });

  function fileName(ms) {
    var d = new Date(ms);
    return "carmind-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" + pad(d.getHours()) +
      pad(d.getMinutes()) + ".json";
  }

  // True only once a sealed, validated, size-checked file was handed to the browser as a download.
  async function exportConversation(conv) {
    var c = S.copy;
    if (!SECURE) { return false; }
    if (!S.writeKey) {
      if (S.savesState === "locked") { setNotice(c.export_needs_unlock); }
      else { setNotice(c.export_needs_passphrase); $("pass").focus(); }
      return false;
    }
    var plain = plaintext(conv);
    if (!validate(plain)) { setNotice(c.export_invalid); return false; }
    var text = await seal(plain, S.writeKey), blob = new Blob([text], { type: "application/json" });
    if (blob.size > S.lim.import_bytes) { setNotice(c.export_too_large); return false; }
    var url = URL.createObjectURL(blob), link = el("a");
    link.href = url;
    link.download = fileName(conv.created_at);
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    return true;
  }

  async function removeConversation(conv) {
    if (conv === S.open || S.pending.has(conv)) { return; }
    if (!(await exportConversation(conv))) { return; }
    // A save still in flight settles first, so the removal acts on the stored record.
    while (conv.saving && conv.saved) { await conv.saved; }
    if (conv.stored !== null) {
      var result = await cas(conv.id, conv.stored, null);
      if (result !== "ok") { renderChats(); return; }
    }
    S.convs.delete(conv.id);
    renderChats();
  }

  async function importFile(file) {
    var c = S.copy;
    S.importing = null;
    paintSaves();
    if (file.size > S.lim.import_bytes) { setNotice(c.import_too_large); return; }
    var env = null;
    try { env = JSON.parse(await file.text()); } catch (err) { env = null; }
    var parts = isEnvelope(env);
    if (!parts) { setNotice(c.import_not_carmind); return; }
    if (S.baseKey) {
      try { finishImport(await openEnvelope(env, parts, S.baseKey)); return; } catch (err) { /* another passphrase */ }
    }
    S.importing = { env: env, parts: parts };
    setNotice(S.baseKey ? c.import_not_session : c.import_enter_passphrase);
    paintSaves();
    $("import-pass").focus();
  }

  async function importWithPassphrase() {
    var job = S.importing;
    if (!job) { return; }
    var base = await baseKeyFrom(["import-pass"]);
    try {
      var plain = await openEnvelope(job.env, job.parts, base);
      S.importing = null;
      paintSaves();
      finishImport(plain);
    } catch (err) {
      setNotice(S.copy.import_wrong);
    }
  }

  function finishImport(plain) {
    var clean = validate(plain);
    S.importing = null;
    paintSaves();
    if (!clean) { setNotice(S.copy.import_not_carmind); return; }
    var conv = fromClean(clean);
    conv.id = freshId();
    conv.imported = true;
    S.convs.set(conv.id, conv);
    queueSave(conv);
    openConversation(conv);
    toggleDrawer(false);
  }

  /* ---------- 10. wiring ---------- */
  $("menu").addEventListener("click", function () { toggleDrawer(true); });
  $("veil").addEventListener("click", function () { toggleDrawer(false); });
  $("new").addEventListener("click", function () { if (S.cfg) { newConversation(); } });
  $("newDrawer").addEventListener("click", function () { if (S.cfg) { newConversation(); } });
  $("ask").addEventListener("click", function () { if (S.cfg) { send(); } });
  $("problem").addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (S.cfg) { send(); } }
  });
  $("problem").addEventListener("input", function (e) {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(104, e.target.scrollHeight) + "px";
  });
  $("lang").addEventListener("change", function (e) {
    S.lang = e.target.value;
    applyDir();
    S.detectSeq++;
    setMic(false);
    if (SECURE) { detect(); }
  });
  $("mic").addEventListener("pointerdown", pressStart);
  $("mic").addEventListener("pointermove", pressMove);
  $("mic").addEventListener("pointerup", function () { pressEnd(false); });
  $("mic").addEventListener("pointercancel", function () { pressEnd(false); });
  $("mic").addEventListener("contextmenu", function (e) { e.preventDefault(); });
  $("core").addEventListener("click", function () { pressEnd(true); });
  $("stopwide").addEventListener("click", function () { pressEnd(true); });

  $("customer").addEventListener("change", function () {
    var conv = S.open, c = own(S.customers, $("customer").value);
    if (inputsLocked(conv)) { paintInputs(conv); return; }
    conv.caller.customer_id = c ? c.id : "";
    conv.caller.caller_number = c ? c.phone : "";
    conv.caller.plate = "";
    paintInputs(conv);
    renderCustomer(c, false);
  });
  $("caller").addEventListener("input", function () {
    var conv = S.open;
    if (inputsLocked(conv)) { paintInputs(conv); return; }
    conv.caller.caller_number = $("caller").value;
    conv.caller.plate = "";
    paintInputs(conv);
    renderCustomer(typedMatch(conv.caller.caller_number), false);
  });
  $("plate").addEventListener("input", function () {
    var conv = S.open;
    if (inputsLocked(conv)) { paintInputs(conv); return; }
    conv.caller.plate = $("plate").value;
  });
  $("after-hours").addEventListener("change", function () {
    var conv = S.open;
    if (inputsLocked(conv)) { paintInputs(conv); return; }
    conv.caller.after_hours = $("after-hours").checked;
  });
  Array.prototype.forEach.call(document.querySelectorAll("button.example"), function (button) {
    button.addEventListener("click", function () { $("problem").value = button.textContent; toggleDrawer(false); });
  });

  $("set-pass").addEventListener("click", setPassphrase);
  $("unlock").addEventListener("click", unlock);
  $("force").addEventListener("click", force);
  $("import").addEventListener("click", function () { $("import-file").click(); });
  $("import-file").addEventListener("change", function () {
    var file = $("import-file").files[0];
    $("import-file").value = "";
    if (file) { importFile(file); }
  });
  $("import-go").addEventListener("click", importWithPassphrase);

  boot();
})();
