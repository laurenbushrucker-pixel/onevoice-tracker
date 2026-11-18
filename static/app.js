"use strict";

// ------------------------------------------------------------------ state
const state = {
  config: null,
  session: null,
  status: null,
  tracked: [],
  updates: [],
  agenda: [],
  stageFilter: "",
  find: { offset: 0, total: 0, results: [] },
  nb: { offset: 0, total: 0, results: [] },
  press: { offset: 0, total: 0, results: [] },
  calWeek: null,
  drawer: null, // { session, bill, data }
  editing: null, // priority id whose keywords are open
};
const VIEWS = ["home", "newbills", "calendar", "press", "agenda", "mine", "find", "updates", "settings", "more", "scoreboard", "goals", "meetings", "report", "directory"];
const STAGE_ORDER = ["introduced", "committee", "floor", "crossed", "passed", "governor", "law"];
const PROGRESS_LABELS = ["Filed", "Committee", "Floor", "2nd chamber", "Passed", "Governor", "Enacted"];
const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };
const TF_SHORT = {
  "Energy and Natural Resources": "Energy",
  "General Government and Legal Reform": "Gov’t",
  "Health and Human Services": "Health",
  "Economic Development": "Econ Dev",
  "Tourism and Quality of Life": "Tourism",
  "Transportation, Infrastructure and Aerospace": "Transport",
  "Workforce and Talent Strategies": "Workforce",
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function ago(iso) {
  if (!iso) return "never";
  const mins = Math.round((Date.now() - new Date(iso)) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const h = Math.round(mins / 60);
  if (h < 48) return `${h} hr ago`;
  return fmtDate(iso);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.reload(); throw new Error("Please sign in again."); }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isError ? 7000 : 3000);
}

function stagePill(stage, label, closed) {
  const lbl = label || (state.config.stages.find((s) => s.key === stage) || {}).label || "Unknown";
  const ended = closed && !["law", "vetoed", "failed"].includes(stage);
  return `<span class="pill st-${esc(stage)}">${esc(lbl)}</span>${ended ? ` <span class="pill closed" title="The legislature adjourned sine die without final action">Session ended</span>` : ""}`;
}

function optionList(values, selected, labeler = cap) {
  return values.map((v) => `<option value="${esc(v)}"${v === selected ? " selected" : ""}>${esc(labeler(v))}</option>`).join("");
}

function summarySource(model) {
  if (!model) return "";
  if (model.startsWith("claude")) return "AI summary";
  if (model === "official") return "Official staff summary";
  if (model === "caption") return "From the bill's description";
  return "Summary";
}
function summaryCell(r) {
  if (r.summary) return `<p class="ai-summary">${esc(r.summary)}</p>${r.summary_model ? `<p class="sum-source">${esc(summarySource(r.summary_model))}</p>` : ""}`;
  return `<p class="ai-missing">No summary yet. <button class="linkish" type="button" data-summarize="${esc(r.bill)}">Get summary</button></p>`;
}
function officialTitle(r) {
  return r.title ? `<p class="official-title">Official title: ${esc(r.title)}</p>` : "";
}
function bindSummarize(root) {
  root.querySelectorAll("[data-summarize]").forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const bill = b.dataset.summarize;
    const p = b.closest(".ai-missing");
    p.textContent = "Getting the summary from oklegislature.gov…";
    try {
      const s = await api(`/api/bill/${state.session}/${bill}/summary`, { method: "POST" });
      [state.tracked, state.find.results].forEach((list) => list.forEach((r) => { if (r.bill === bill) { r.summary = s.summary; r.summary_model = s.model; } }));
      const out = document.createElement("p");
      out.className = "ai-summary"; out.textContent = s.summary;
      const src = document.createElement("p");
      src.className = "sum-source"; src.textContent = summarySource(s.model);
      p.replaceWith(out, src);
    } catch (err) { p.textContent = err.message; }
  }));
}
function priorityById(id) { return state.config.agenda.find((p) => p.id === id); }
function shortLabel(p) { return `${TF_SHORT[p.task_force] || p.task_force} ${p.number}`; }
function agendaTags(list) {
  if (!list || !list.length) return "";
  return `<div class="agenda-tags">${list.map((a) => {
    const p = priorityById(a.id);
    return p ? `<span class="agenda-tag${a.review === "not_relevant" ? " dismissed" : ""}" title="${esc(p.task_force)} #${p.number}: ${esc(p.title)} (matched: ${esc(a.keywords.join(", "))})${a.review === "not_relevant" ? " (marked not relevant)" : ""}">${esc(shortLabel(p))}</span>` : "";
  }).join("")}</div>`;
}

function snippetHtml(sn) {
  if (!sn) return "";
  return `<p class="snippet">Found: ${esc(sn).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>")}</p>`;
}
function positionPicker(r) {
  const pos = r.tracked_position || "";
  return `<div class="pos-pick" data-bill="${esc(r.bill)}">
    <label>Chamber position
      <select class="pos-select${pos ? " pos-" + esc(pos) : ""}" aria-label="Chamber position on ${esc(r.display)}">
        <option value="" ${pos ? "" : "selected"}>Choose a position…</option>
        ${["support", "oppose", "monitor", "neutral"].map((p) => `<option value="${p}" ${p === pos ? "selected" : ""}>${cap(p)}</option>`).join("")}
      </select></label>
    <label class="pos-why-label">Why <input class="pos-why" maxlength="1000" placeholder="Optional: the reason, for the record" value="${esc(r.position_reason || "")}" ${pos ? "" : "disabled"}></label>
  </div>`;
}
function bindPositions(root, list) {
  root.querySelectorAll(".pos-pick").forEach((box) => {
    const bill = box.dataset.bill, sel = box.querySelector(".pos-select"), why = box.querySelector(".pos-why");
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      try {
        const row = state.tracked.find((t) => t.bill === bill)
          ? await api(`/api/tracked/${state.session}/${bill}`, { method: "PATCH", body: { position: sel.value } })
          : await api("/api/tracked", { method: "POST", body: { session: state.session, bill, position: sel.value } });
        sel.className = "pos-select pos-" + sel.value;
        why.disabled = false;
        list.forEach((r) => { if (r.bill === bill) r.tracked_position = sel.value; });
        toast(`${row.display}: Chamber position set to ${cap(sel.value)}. It’s on My bills, and you’ll get an email when it moves.`);
        loadTracked();
      } catch (err) { toast(err.message, true); }
    });
    let saved = why.value;
    const saveWhy = async () => {
      if (why.value === saved || why.disabled) return;
      try {
        await api(`/api/tracked/${state.session}/${bill}`, { method: "PATCH", body: { reason: why.value } });
        saved = why.value;
        list.forEach((r) => { if (r.bill === bill) r.position_reason = why.value; });
        toast("Reason saved");
      } catch (err) { toast(err.message, true); }
    };
    why.addEventListener("blur", saveWhy);
    why.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); why.blur(); } });
  });
}
function miniProgress(pr, r) {
  if (!pr) return `<p class="last"><b>Status:</b> ${esc(r.stage_label || "")}${r.last_action ? ` · ${esc(r.last_action)}` : ""}</p>`;
  return `<div class="mini-prog"><span class="prog-pct st-${esc(pr.status)}">${pr.percent}%</span>
    <div class="prog-bar st-${esc(pr.status)}">${pr.steps.map((st) => `<i class="${st.done ? "on" : st.current ? "cur" : ""}" title="${esc(st.label)}"></i>`).join("")}</div>
    <span class="mini-where">${esc(pr.where)}</span></div>
    <p class="last"><b>Last action:</b> ${esc(r.last_action || "None yet")}${r.last_action_date ? " · " + fmtDate(r.last_action_date) : ""}</p>`;
}

function reviewButtons(bill, pid, verdict) {
  return `<span class="review" data-bill="${esc(bill)}" data-pid="${esc(pid)}">
    <button type="button" class="rv rv-yes" data-verdict="relevant" aria-pressed="${verdict === "relevant"}">✓ Relevant</button>
    <button type="button" class="rv rv-no" data-verdict="not_relevant" aria-pressed="${verdict === "not_relevant"}">✕ Not relevant</button></span>`;
}
function bindReviews(root, onChange) {
  root.querySelectorAll(".review .rv").forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const box = b.closest(".review"), bill = box.dataset.bill, pid = box.dataset.pid;
    const verdict = b.getAttribute("aria-pressed") === "true" ? null : b.dataset.verdict;
    try {
      await api("/api/review", { method: "POST", body: { session: state.session, bill, priority_id: pid, verdict } });
      box.querySelectorAll(".rv").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.verdict === verdict)));
      [state.find.results, state.tracked].forEach((list) => list.forEach((r) => {
        if (r.bill === bill) (r.agenda || []).forEach((a) => { if (a.id === pid) a.review = verdict; });
      }));
      if (onChange) onChange(bill, pid, verdict);
      loadAgenda();
    } catch (err) { toast(err.message, true); }
  }));
}

// ------------------------------------------------------------------ home
async function loadHome() {
  let h;
  try { h = await api(`/api/home?session=${state.session}`); } catch { return; }
  const hour = new Date().getHours();
  $("#home-hello").textContent = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
  $("#home-sub").textContent = h.to_review ? `${h.to_review} new OneVoice bill${h.to_review === 1 ? "" : "s"} to review` : "You’re all caught up.";
  $("#tile-bills").textContent = h.new_bills ? `${h.new_bills} new this week${h.to_review ? ` · ${h.to_review} to review` : ""}` : "Bills matching the agenda";
  $("#tile-cal").textContent = h.meetings_this_week ? `${h.meetings_this_week} meeting${h.meetings_this_week === 1 ? "" : "s"} this week` : "Committee meetings and agendas";
  $("#tile-press").textContent = h.press_new ? `${h.press_new} new this week` : "Senate, House, Governor, agencies";
  $("#tile-mine").textContent = h.tracked ? `${h.tracked} tracked${h.updates ? ` · ${h.updates} update${h.updates === 1 ? "" : "s"}` : ""}` : "Bills with a Chamber position";
  if (h.goals_line) $("#tile-goals").textContent = h.goals_line;
  if (h.meetings_logged) $("#tile-meetings").textContent = `${h.meetings_logged} logged in ${h.goal_year}`;
  $("#nav-dot-bills").hidden = !h.to_review;
  $("#home-hearings").hidden = !h.your_hearings.length;
  $("#home-hearings-list").innerHTML = h.your_hearings.map((m) => `
    <li><a href="#calendar"><b>${esc(m.your_bills.join(", "))}</b> · ${esc(m.chamber)} ${esc(m.committee)}
      <span class="date">${esc(fmtWhen(m.start, m.time_text))}${m.location ? " · " + esc(m.location) : ""}</span></a></li>`).join("");
  renderGetApp();
}
function fmtWhen(iso, fallback) {
  if (!iso) return fallback || "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" }) + " · " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

// ------------------------------------------------------------------ calendar
function mondayOf(d) {
  const x = new Date(d); x.setHours(0, 0, 0, 0);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}
function isoDate(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }
async function loadCalendar() {
  if (!state.calWeek) state.calWeek = mondayOf(new Date());
  const wk = isoDate(state.calWeek);
  const end = new Date(state.calWeek); end.setDate(end.getDate() + 6);
  const thisWeek = isoDate(mondayOf(new Date())) === wk;
  $("#cal-week").textContent = thisWeek ? "This week" : `Week of ${state.calWeek.toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  $("#cal-days").innerHTML = `<p class="hint">Loading meetings…</p>`;
  let data;
  try { data = await api(`/api/calendar?session=${state.session}&week=${wk}`); } catch (e) { $("#cal-days").innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  $("#cal-updated").textContent = `${state.calWeek.toLocaleDateString(undefined, { month: "short", day: "numeric" })} – ${end.toLocaleDateString(undefined, { month: "short", day: "numeric" })}` + (data.updated ? ` · checked ${ago(data.updated)}` : "");
  const only = $("#cal-only").checked;
  const days = [];
  for (let i = 0; i < 7; i++) {
    const d = new Date(state.calWeek); d.setDate(d.getDate() + i);
    const key = isoDate(d);
    const ms = data.meetings.filter((m) => (m.start || "").slice(0, 10) === key && (!only || m.bills.some((b) => b.onevoice || b.watch)));
    const dl = data.deadlines.filter((x) => x.date === key);
    if (!ms.length && !dl.length) continue;
    days.push(`<section class="cal-day${key === isoDate(new Date()) ? " today" : ""}">
      <h3>${d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}${key === isoDate(new Date()) ? ' <span class="today-pill">Today</span>' : ""}</h3>
      ${dl.map((x) => `<div class="deadline">📌 ${esc(x.label)}</div>`).join("")}
      ${ms.map(meetingCard).join("")}
    </section>`);
  }
  $("#cal-days").innerHTML = days.join("") || `<div class="empty"><h2>${only ? "No meetings with OneVoice bills this week" : "No committee meetings posted for this week"}</h2>
    <p>${thisWeek ? "Meeting notices usually go up a few days ahead. New ones appear here within 20 minutes of being posted." : "Try another week."}</p></div>`;
  $("#cal-days").querySelectorAll("[data-open-bill]").forEach((b) => b.addEventListener("click", () => openBill(state.session, b.dataset.openBill)));
}
function meetingCard(m) {
  const cancelled = /cancel|postpon/i.test(`${m.status} ${m.title}`);
  const revised = /revis/i.test(m.title);
  const time = m.start ? new Date(m.start).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : (m.time_text || "");
  const bills = m.bills.slice().sort((a, b) => (b.watch ? 2 : b.onevoice ? 1 : 0) - (a.watch ? 2 : a.onevoice ? 1 : 0));
  return `<article class="meeting${cancelled ? " cancelled" : ""}">
    <div class="m-time">${esc(time)}</div>
    <div class="m-body">
      <div class="m-head"><span class="chamber-pill ch-${esc(m.chamber.toLowerCase())}">${esc(m.chamber)}</span>
        <b>${esc(m.committee)}</b>${cancelled ? ' <span class="pill st-vetoed">Cancelled</span>' : revised ? ' <span class="pill st-passed">Revised</span>' : ""}</div>
      <div class="m-where">${esc(m.location || "")}</div>
      ${bills.length ? `<div class="m-bills">${bills.map((b) => `<button type="button" class="bill-chip${b.watch ? " watch" : b.onevoice ? " ov" : ""}" data-open-bill="${esc(b.bill)}" title="${esc(b.title)}">${esc(b.display)}${b.watch ? " ★" : ""}</button>`).join("")}</div>` : (m.agenda_url ? "" : `<div class="hint">Agenda not posted yet.</div>`)}
      ${m.removed.length ? `<div class="hint">Removed: ${esc(m.removed.join(", "))}</div>` : ""}
      <div class="m-links">${m.agenda_url ? `<a class="btn btn-sm" href="${esc(m.agenda_url)}" target="_blank" rel="noopener">Agenda (PDF)</a>` : ""}
        <a class="btn btn-ghost btn-sm" href="${esc(m.page_url)}" target="_blank" rel="noopener">Watch / details</a></div>
    </div></article>`;
}

// ------------------------------------------------------------------ press
async function runPress(reset) {
  if (reset) state.press = { offset: 0, total: 0, results: [] };
  const p = new URLSearchParams({ session: state.session, q: $("#press-q").value.trim(), source: $("#press-source").value,
    show: $("#press-show").value, offset: state.press.offset, limit: 30 });
  $("#press-meta").textContent = "Loading…";
  try {
    const d = await api(`/api/press?${p}`);
    if ($("#press-source").options.length <= 1) $("#press-source").innerHTML += d.sources.map((x) => `<option>${esc(x)}</option>`).join("");
    state.press.total = d.total; state.press.results = state.press.results.concat(d.results); state.press.offset = state.press.results.length;
    $("#press-meta").textContent = d.total ? `${d.total.toLocaleString()} release${d.total === 1 ? "" : "s"}, newest first${d.updated ? ` · checked ${ago(d.updated)}` : ""}` : "No press releases match.";
    $("#press-list").innerHTML = state.press.results.map((r) => `
      <li class="press-card">
        <div class="press-top"><span class="src-pill">${esc(r.source)}</span><span class="date">${fmtDate((r.published || r.first_seen || "").slice(0, 10))}</span></div>
        <a class="press-title" href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>
        ${r.excerpt ? `<p class="press-excerpt">${esc(r.excerpt.slice(0, 220))}${r.excerpt.length > 220 ? "…" : ""}</p>` : ""}
        ${(r.bills.length || r.priorities.length) ? `<div class="m-bills">${r.bills.map((b) => `<button type="button" class="bill-chip${b.watch ? " watch" : ""}" data-open-bill="${esc(b.bill)}">${esc(b.display)}${b.watch ? " ★" : ""}</button>`).join("")}${r.priorities.map((p2) => `<span class="agenda-tag">${esc(p2.label)}</span>`).join("")}</div>` : ""}
      </li>`).join("");
    $("#press-more").hidden = state.press.results.length >= state.press.total;
    $("#press-list").querySelectorAll("[data-open-bill]").forEach((b) => b.addEventListener("click", () => openBill(state.session, b.dataset.openBill)));
  } catch (e) { $("#press-meta").textContent = e.message; }
}

// ------------------------------------------------------- phone app & notifications
let swReg = null;
async function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  try {
    swReg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    navigator.serviceWorker.addEventListener("message", (e) => {
      if (e.data && e.data.type === "open" && e.data.url) { const u = new URL(e.data.url); location.hash = u.hash || "#home"; route(); }
    });
  } catch { /* not available here; the site still works */ }
}
const isStandalone = () => window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isAndroid = () => /android/i.test(navigator.userAgent);
let deferredInstall = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferredInstall = e; renderGetApp(); renderInstallHelp(); });

function installSteps() {
  if (isStandalone()) return `<p class="ok-line">✓ Installed on this device.</p>`;
  if (isIOS()) return `<ol class="steps"><li>Open this page in <b>Safari</b>.</li><li>Tap the <b>Share</b> button <span aria-hidden="true">(the square with an arrow)</span>.</li><li>Scroll down and tap <b>Add to Home Screen</b>, then <b>Add</b>.</li><li>Open <b>OneVoice</b> from your home screen and turn on notifications.</li></ol>`;
  if (deferredInstall) return `<button class="btn" type="button" id="install-btn">Install the app</button>`;
  if (isAndroid()) return `<ol class="steps"><li>Tap Chrome’s <b>⋮</b> menu.</li><li>Tap <b>Install app</b> (or <b>Add to Home screen</b>).</li></ol>`;
  return `<p class="hint">On your phone, open this site and add it to your home screen: Safari ▸ Share ▸ Add to Home Screen on iPhone, or Chrome ▸ ⋮ ▸ Install app on Android. On a computer, Chrome and Edge show an install button in the address bar.</p>`;
}
function bindInstallBtn() {
  const b = document.getElementById("install-btn");
  if (b) b.addEventListener("click", async () => { deferredInstall.prompt(); await deferredInstall.userChoice; deferredInstall = null; renderGetApp(); renderInstallHelp(); });
}
function renderInstallHelp() { $("#install-help").innerHTML = `<h3 class="sub-h">Get the app</h3>${installSteps()}`; bindInstallBtn(); }
async function renderGetApp() {
  const sub = await currentSubscription();
  const box = $("#get-app");
  if (isStandalone() && sub) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = isStandalone()
    ? `<b>Turn on notifications</b><p class="hint">Get new OneVoice bills, hearings and press releases on your lock screen.</p><a class="btn" href="#settings">Set up notifications</a>`
    : `<b>Put OneVoice on your home screen</b><p class="hint">It opens like an app and can send notifications.</p>${installSteps()}`;
  bindInstallBtn();
}
async function currentSubscription() {
  if (!swReg || !("PushManager" in window)) return null;
  try { return await swReg.pushManager.getSubscription(); } catch { return null; }
}
function b64ToUint8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}
async function renderPush() {
  const status = $("#push-status"), toggle = $("#push-toggle"), test = $("#push-test"), kinds = $("#push-kinds");
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    status.textContent = isIOS() && !isStandalone()
      ? "On iPhone, notifications work once OneVoice is added to your home screen (iOS 16.4 or later). Follow the steps above, then open it from the home screen."
      : "This browser can’t receive notifications.";
    toggle.hidden = test.hidden = kinds.hidden = true; return;
  }
  if (!swReg) { try { swReg = await navigator.serviceWorker.ready; } catch { /* */ } }
  const sub = await currentSubscription();
  let info = { subscribed: false, prefs: null };
  if (sub) { try { info = await api("/api/push/status", { method: "POST", body: { endpoint: sub.endpoint } }); } catch { /* */ } }
  const on = sub && info.subscribed;
  if (Notification.permission === "denied") {
    status.textContent = "Notifications are blocked for this site. Allow them in your phone’s or browser’s settings for this site, then come back.";
    toggle.hidden = true;
  } else {
    status.textContent = on ? "✓ Notifications are on for this device. Choose what to get:" : "Get alerts on this device’s lock screen, even when the app is closed.";
    toggle.hidden = false; toggle.textContent = on ? "Turn off notifications" : "Turn on notifications";
    toggle.className = on ? "btn btn-ghost" : "btn";
  }
  test.hidden = !on; kinds.hidden = !on;
  if (on) {
    const k = (await api("/api/push/key")).kinds;
    kinds.innerHTML = Object.entries(k).map(([key, label]) => `<label class="check" for="pk-${key}"><input type="checkbox" id="pk-${key}" data-kind="${key}" ${!info.prefs || info.prefs[key] !== false ? "checked" : ""}> ${esc(label)}</label>`).join("");
    kinds.querySelectorAll("[data-kind]").forEach((c) => c.addEventListener("change", async () => {
      const prefs = Object.fromEntries([...kinds.querySelectorAll("[data-kind]")].map((x) => [x.dataset.kind, x.checked]));
      try { await api("/api/push/prefs", { method: "POST", body: { endpoint: sub.endpoint, prefs } }); toast("Saved"); } catch (e) { toast(e.message, true); }
    }));
  }
}
async function togglePush() {
  const sub = await currentSubscription();
  try {
    if (sub) {
      await api("/api/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } });
      await sub.unsubscribe();
      toast("Notifications turned off on this device");
    } else {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { toast("Notifications weren’t allowed. You can allow them in your settings later.", true); renderPush(); return; }
      const { key } = await api("/api/push/key");
      const reg = swReg || await navigator.serviceWorker.ready;
      const fresh = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToUint8(key) });
      const label = isIOS() ? "iPhone" : isAndroid() ? "Android phone" : "Computer";
      await api("/api/push/subscribe", { method: "POST", body: { subscription: fresh.toJSON(), label } });
      toast("Notifications are on for this device");
    }
  } catch (e) { toast(e.message || "Couldn’t change notifications", true); }
  renderPush(); renderGetApp();
}

// ------------------------------------------------------- press sources
async function loadSources() {
  try {
    const d = await api("/api/press/sources");
    $("#src-list").innerHTML = d.sources.map((x) => `<li><b>${esc(x.name)}</b> <span class="hint">${esc(x.url.replace("https://", ""))}</span>
      ${x.id.startsWith("c") ? `<button class="linkish" type="button" data-src-del="${esc(x.id)}">Remove</button>` : ""}</li>`).join("");
    $("#src-list").querySelectorAll("[data-src-del]").forEach((b) => b.addEventListener("click", async () => {
      await api(`/api/press/sources/${b.dataset.srcDel}`, { method: "DELETE" }); toast("Source removed"); loadSources();
    }));
  } catch { /* */ }
}

function bindAppFeatures() {
  $("#cal-prev").addEventListener("click", () => { state.calWeek.setDate(state.calWeek.getDate() - 7); loadCalendar(); });
  $("#cal-next").addEventListener("click", () => { state.calWeek.setDate(state.calWeek.getDate() + 7); loadCalendar(); });
  $("#cal-today").addEventListener("click", () => { state.calWeek = mondayOf(new Date()); loadCalendar(); });
  $("#cal-only").addEventListener("change", loadCalendar);
  $("#press-form").addEventListener("submit", (e) => { e.preventDefault(); runPress(true); });
  ["#press-source", "#press-show"].forEach((x) => $(x).addEventListener("change", () => runPress(true)));
  $("#press-more").addEventListener("click", () => runPress(false));
  $("#push-toggle").addEventListener("click", togglePush);
  $("#push-test").addEventListener("click", async () => {
    const sub = await currentSubscription();
    try { await api("/api/push/test", { method: "POST", body: { endpoint: sub.endpoint } }); toast("Test notification sent"); } catch (e) { toast(e.message, true); }
  });
  $("#src-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/press/sources", { method: "POST", body: { url: $("#src-url").value.trim(), name: $("#src-name").value.trim() } });
      toast(`Added. Found ${r.found} recent release${r.found === 1 ? "" : "s"}; new ones will appear within 15 minutes.`);
      $("#src-url").value = ""; $("#src-name").value = ""; loadSources();
    } catch (err) { toast(err.message, true); }
  });
}

// ------------------------------------------------------------------ boot
async function boot() {
  state.config = await api("/api/config");
  let saved = null;
  try { saved = localStorage.getItem("ovt.session"); } catch { /* storage blocked */ }
  const codes = state.config.sessions.map((s) => s.code);
  state.session = codes.includes(saved) ? saved : state.config.default_session;

  $("#session").innerHTML = state.config.sessions.map((s) => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join("");
  $("#session").value = state.session;
  $("#mine-position").innerHTML += optionList(state.config.positions, "");
  $("#mine-priority").innerHTML += optionList(state.config.priorities, "");
  $("#find-stage").innerHTML += state.config.stages.map((s) => `<option value="${s.key}">${esc(s.label)}</option>`).join("");
  fillAgendaSelects();
  $("#agenda-name").textContent = state.config.agenda_name;

  $("#session-more").innerHTML = $("#session").innerHTML;
  $("#session-more").value = state.session;
  $("#session-more").addEventListener("change", (e) => { $("#session").value = e.target.value; $("#session").dispatchEvent(new Event("change")); });
  registerServiceWorker();
  bindEvents();
  bindAppFeatures();
  await loadSession();
  route();
  setInterval(pollStatus, 4000);
}

function fillAgendaSelects() {
  const opts = state.config.agenda.map((p) => `<option value="${p.id}">${esc(shortLabel(p))}: ${esc(p.title)}</option>`).join("");
  $("#nb-priority").innerHTML = `<option value="">All agenda priorities</option>${opts}`;
  $("#find-agenda").innerHTML = `<option value="">All bills</option><option value="any">Matches any agenda item</option>${opts}`;
  $("#mine-agenda").innerHTML = `<option value="">Any agenda item</option><option value="any">Matches the agenda</option>${opts}`;
}

function bindEvents() {
  window.addEventListener("hashchange", route);
  $("#session").addEventListener("change", async (e) => {
    state.session = e.target.value;
    try { localStorage.setItem("ovt.session", state.session); } catch { /* ignore */ }
    state.find = { offset: 0, total: 0, results: [] };
    state.nb = { offset: 0, total: 0, results: [] };
    await loadSession();
    if (currentView() === "newbills") runNewBills(true);
    if (currentView() === "find") runSearch(true);
  });
  $("#btn-refresh").addEventListener("click", () => startJob(`/api/refresh?session=${state.session}`, "Checking tracked bills on oklegislature.gov"));
  $("#btn-filings").addEventListener("click", () => startJob(`/api/filings?session=${state.session}`, "Looking for newly filed bills"));
  $("#btn-index").addEventListener("click", () => startJob(`/api/index?session=${state.session}`, "Building the search index"));
  ["#mine-q", "#mine-position", "#mine-priority", "#mine-agenda", "#mine-sort"].forEach((s) => $(s).addEventListener("input", renderMine));
  $("#quick-add").addEventListener("submit", async (e) => {
    e.preventDefault();
    const v = $("#quick-bill").value.trim();
    if (!v) return;
    if (await track(v)) $("#quick-bill").value = "";
  });
  $("#export").addEventListener("click", (e) => {
    e.preventDefault();
    window.location.href = `/api/export.csv?session=${state.session}`;
  });
  $("#find-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(true); });
  $("#nb-form").addEventListener("submit", (e) => { e.preventDefault(); runNewBills(true); });
  ["#nb-priority", "#nb-review", "#nb-position"].forEach((s) => $(s).addEventListener("change", () => runNewBills(true)));
  $("#nb-more").addEventListener("click", () => runNewBills(false));
  ["#find-agenda", "#find-dismissed", "#find-subject", "#find-chamber", "#find-stage", "#find-sort"].forEach((s) => $(s).addEventListener("change", () => {
    if (location.hash.startsWith("#bills-")) history.replaceState(null, "", "#find");
    $("#find-back").hidden = true;
  if (h.startsWith("bill-")) {  // a notification or link to one bill: #bill-2700-HB1234
    const [, sess, bill] = h.split("-");
    history.replaceState(null, "", "#newbills");
    if (sess && bill) openBill(sess, bill);
  }
    runSearch(true);
  }));
  $("#find-more").addEventListener("click", () => runSearch(false));
  $("#mark-seen").addEventListener("click", async () => {
    await api(`/api/updates/seen?session=${state.session}`, { method: "POST", body: {} });
    await loadUpdates(); await loadTracked();
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  $("#drawer-refresh").addEventListener("click", () => state.drawer && openBill(state.drawer.session, state.drawer.bill, true));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#drawer").hidden) closeDrawer(); });
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#checkin-now").addEventListener("click", async () => {
    $("#checkin-now").disabled = true;
    try { await api("/api/checkin/send", { method: "POST" }); toast("Check-in sent. Look for it in your inbox."); loadHealth(); }
    catch (err) { toast(err.message, true); } finally { $("#checkin-now").disabled = false; }
  });
  $("#s-test").addEventListener("click", testEmail);
  $("#agenda-reset").addEventListener("click", confirmReset);
  bindNewFeatures();
}

async function loadSession() {
  await Promise.all([loadTracked(), loadUpdates(), pollStatus(), loadSubjects(), loadAgenda(), loadSettings()]);
}

async function startJob(url, message) {
  try {
    await api(url, { method: "POST" });
    toast(message);
    pollStatus();
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ routing
function currentView() {
  const h = location.hash.replace("#", "");
  if (h.startsWith("bills-")) return "find";
  if (h.startsWith("bill-")) return "newbills";
  if (h.startsWith("legislator-")) return "directory";
  return VIEWS.includes(h) ? h : "home";
}
function route() {
  hideTip();
  const view = currentView();
  const h = location.hash.replace("#", "");
  if (h.startsWith("bills-") && priorityById(h.slice(6))) {
    // A priority's bill list: show every bill matching that agenda item, newest first.
    $("#find-agenda").value = h.slice(6);
    $("#find-q").value = ""; $("#find-subject").value = ""; $("#find-stage").value = ""; $("#find-chamber").value = "";
    $("#find-sort").value = "newest";
    $("#find-dismissed").value = "";
    document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-find"));
    document.querySelectorAll(".tabs a").forEach((a) => a.setAttribute("aria-current", a.dataset.view === "find" ? "page" : "false"));
    $("#find-back").hidden = false;
    window.scrollTo(0, 0);
    runSearch(true);
    return;
  }
  $("#find-back").hidden = true;
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  document.querySelectorAll(".tabs a").forEach((a) => a.setAttribute("aria-current", a.dataset.view === view ? "page" : "false"));
  if (view === "find" && !state.find.results.length) runSearch(true);
  if (view === "newbills" && !state.nb.results.length) runNewBills(true);
  if (view === "home") loadHome();
  if (view === "calendar") loadCalendar();
  if (view === "press" && !state.press.results.length) runPress(true);
  if (view === "scoreboard") loadScoreboard();
  if (view === "goals") loadGoals();
  if (view === "meetings") loadMeetings();
  if (view === "report") loadReport();
  if (view === "directory") { const h2 = location.hash.replace("#", ""); h2.startsWith("legislator-") ? openLegislator(h2.slice(11)) : showDirectory(); }
  document.querySelectorAll(".bottom-nav a").forEach((a) => a.setAttribute("aria-current",
    a.dataset.view === view || (a.dataset.view === "more" && ["find", "mine", "agenda", "updates", "settings", "more", "scoreboard", "goals", "meetings", "report", "directory"].includes(view)) ? "page" : "false"));
  window.scrollTo(0, 0);
}

// ------------------------------------------------------------------ status
let lastJobFinished = null;
let healthTick = 0;
async function pollStatus() {
  if (++healthTick % 8 === 0) loadHealth();
  try {
    state.status = await api(`/api/status?session=${state.session}`);
  } catch { return; }
  const s = state.status, job = s.job || {};
  const bar = $("#statusbar");
  if (job.running) {
    const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
    const what = { index: "Building search index", filings: "Looking for newly filed bills", refresh: "Checking tracked bills", summaries: "Getting bill summaries", texts: "Reading bill texts", calendar: "Checking committee meetings", press: "Checking press releases" }[job.kind] || "Working";
    bar.innerHTML = `${what}: ${esc(job.label || "starting")} (${job.done}/${job.total || "?"})<span class="bar"><i style="width:${pct}%"></i></span>`;
  } else {
    const parts = [];
    if (s.filings) parts.push(`new filings checked ${ago(s.filings.at)}`);
    if (s.indexed) parts.push(`${s.indexed.count.toLocaleString()} measures indexed ${ago(s.indexed.at)}`);
    else if (s.bills) parts.push(`${s.bills.toLocaleString()} measures known`);
    parts.push(`tracked bills checked ${ago(s.last_auto_refresh || newestCheck())}`);
    if (s.closed) parts.push("this session has adjourned sine die");
    if (s.summaries_waiting) parts.push(`${s.summaries_waiting >= 1000 ? "1,000+" : s.summaries_waiting} summar${s.summaries_waiting === 1 ? "y" : "ies"} queued`);
    bar.textContent = cap(parts.join(" · ")) + ".";
    
    if (job.error) bar.textContent += ` Last update failed: ${job.error}`;
  }
  ["#btn-refresh", "#btn-index", "#btn-filings"].forEach((b) => ($(b).disabled = !!job.running));
  if (job.finished && job.finished !== lastJobFinished) {
    const firstSeen = lastJobFinished === null;
    lastJobFinished = job.finished;
    if (!firstSeen) {
      await Promise.all([loadTracked(), loadUpdates(), loadSubjects(), loadAgenda(), loadSettings()]);
      if (currentView() === "find") runSearch(true);
      if (currentView() === "newbills") runNewBills(true);
      if (currentView() === "report") loadReport();
      if (job.kind === "legislators" && currentView() === "directory") { DIR.rows = null; showDirectory(); }
      let msg = { legislators: "Legislator directory updated", details: "Bill authors updated", report: "This week’s report dates are up to date", index: "Search index is ready", refresh: "Tracked bills are up to date", filings: "Finished checking for new filings",
        texts: "Bill texts updated", calendar: "Calendar updated", press: "Press releases updated",
        summaries: job.result ? `Updated ${job.result.summarized} summar${job.result.summarized === 1 ? "y" : "ies"}` : "Summaries updated" }[job.kind];
      if (job.kind === "filings" && job.result) {
        const r = job.result;
        msg = `${r.new_bills} new bill${r.new_bills === 1 ? "" : "s"} found, ${r.new_matches} agenda match${r.new_matches === 1 ? "" : "es"}` +
          (r.sent ? `, alert emailed for ${r.sent}` : "") + (r.error ? `. Email failed: ${r.error}` : "");
      }
      const quiet = ["calendar", "press", "texts", "summaries"].includes(job.kind) && !job.error;  // background refreshes
      if (currentView() === "calendar") loadCalendar();
      if (currentView() === "home") loadHome();
      if (!quiet) toast(job.error ? `Update failed: ${job.error}` : msg, !!(job.error || (job.result && job.result.error)));
    }
  }
}
function newestCheck() {
  return state.tracked.map((t) => t.last_checked).filter(Boolean).sort().pop();
}

// ------------------------------------------------------------------ agenda
async function loadAgenda() {
  const data = await api(`/api/agenda?session=${state.session}`);
  state.agenda = data.priorities;
  state.config.agenda = data.priorities;
  const sessionName = ($("#session").selectedOptions[0] || {}).textContent || "";
  $("#agenda-summary").textContent = `${data.bills_matching.toLocaleString()} bill${data.bills_matching === 1 ? "" : "s"} in the ${sessionName} match at least one priority.`;
  renderAgenda();
}

function renderAgenda() {
  const groups = [];
  state.agenda.forEach((p) => {
    let g = groups.find((x) => x.name === p.task_force);
    if (!g) groups.push((g = { name: p.task_force, items: [] }));
    g.items.push(p);
  });
  $("#agenda-list").innerHTML = groups.map((g) => `
    <section class="tf">
      <h3>${esc(g.name)} Task Force</h3>
      <div class="tf-grid">${g.items.map(priorityCard).join("")}</div>
    </section>`).join("");
  $("#agenda-list").querySelectorAll(".pcard").forEach((card) => card.addEventListener("click", (e) => {
    if (e.target.closest("button, a, form, input, textarea, label")) return;
    location.hash = "#bills-" + card.id.slice(2);
  }));
  $("#agenda-list").querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => {
    state.editing = state.editing === b.dataset.edit ? null : b.dataset.edit;
    renderAgenda();
    if (state.editing) { previewKeywords(state.editing); loadNoise(state.editing); }
  }));
  if (state.editing) bindEditor(state.editing);
}

function priorityCard(p) {
  const open = state.editing === p.id;
  return `
    <article class="pcard${p.top ? " top" : ""}${p.enabled ? "" : " disabled"}" id="p-${p.id}">
      <div class="pcard-title"><span class="pcard-num">${p.number}.</span><h4><a class="pcard-link" href="#bills-${p.id}">${esc(p.title)}</a></h4></div>
      ${p.top ? `<div><span class="badge-top">Top priority</span></div>` : ""}
      <p class="summary">${esc(p.summary)}</p>
      <div class="pcard-stats">
        <span><b>${p.matches}</b> matching bill${p.matches === 1 ? "" : "s"}</span>
        ${p.recent ? `<span class="new"><b>${p.recent}</b> new this week</span>` : ""}
        <span><b>${p.tracked}</b> tracked</span>
        ${p.dismissed ? `<span class="muted"><b>${p.dismissed}</b> not relevant</span>` : ""}
        ${p.enabled ? "" : "<span>Matching is off</span>"}
      </div>
      <div class="pcard-actions">
        <a class="btn btn-sm" href="#bills-${p.id}">View ${p.matches} bill${p.matches === 1 ? "" : "s"}</a>
        <button class="btn btn-ghost btn-sm" type="button" data-edit="${p.id}" aria-expanded="${open}">${open ? "Close keywords" : "Edit keywords"}</button>
      </div>
      ${open ? `
      <form class="kw-editor" id="kw-form">
        <label for="kw-list">Keywords (one per line; end with * to match any ending)
          <textarea id="kw-list">${esc(p.keywords.join("\n"))}</textarea></label>
        <label for="kw-exclude">Skip bills whose title also contains (optional)
          <textarea id="kw-exclude" style="min-height:60px">${esc(p.exclude.join("\n"))}</textarea></label>
        <label class="check" for="kw-enabled"><input type="checkbox" id="kw-enabled" ${p.enabled ? "checked" : ""}> Match bills to this priority</label>
        <div class="kw-preview" id="kw-preview">Counting matches…</div>
        <div class="kw-noise" id="kw-noise"></div>
        <div class="row-end"><button class="btn btn-sm" type="submit">Save keywords</button></div>
      </form>` : ""}
    </article>`;
}

async function loadNoise(id) {
  const el = $("#kw-noise");
  if (!el) return;
  try {
    const rows = (await api(`/api/agenda/${id}/noise`)).filter((r) => r.not_relevant);
    el.innerHTML = rows.length
      ? `<b>Keywords behind “Not relevant” marks:</b> ${rows.slice(0, 8).map((r) => `<span class="noise-kw">${esc(r.keyword)} <i>${r.not_relevant}✕${r.relevant ? ` ${r.relevant}✓` : ""}</i></span>`).join(" ")}
         <div class="hint">A keyword with many ✕ and few ✓ is a good one to remove or narrow.</div>`
      : `<span class="hint">As you mark bills Relevant or Not relevant, this shows which keywords cause the most noise.</span>`;
  } catch { el.textContent = ""; }
}

let previewTimer;
function bindEditor(id) {
  const form = $("#kw-form");
  if (!form) return;
  ["#kw-list", "#kw-exclude"].forEach((s) => $(s).addEventListener("input", () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => previewKeywords(id), 400);
  }));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const lines = (s) => $(s).value.split("\n").map((x) => x.trim()).filter(Boolean);
    try {
      await api(`/api/agenda/${id}`, { method: "PUT", body: { keywords: lines("#kw-list"), exclude: lines("#kw-exclude"), enabled: $("#kw-enabled").checked } });
      state.editing = null;
      toast("Keywords saved. Bills that newly match won’t trigger email; only future filings do.");
      await Promise.all([loadAgenda(), loadTracked()]);
    } catch (err) { toast(err.message, true); }
  });
}

async function previewKeywords(id) {
  const el = $("#kw-preview");
  if (!el) return;
  const p = new URLSearchParams({ session: state.session, keywords: $("#kw-list").value, exclude: $("#kw-exclude").value });
  try {
    const r = await api(`/api/agenda/preview?${p}`);
    el.innerHTML = `These keywords match <b>${r.total.toLocaleString()}</b> bill${r.total === 1 ? "" : "s"} in this session.` +
      (r.sample.length ? `<ul>${r.sample.map((s) => `<li><span class="billno">${esc(s.display)}</span> ${esc(s.title.slice(0, 90))}${s.title.length > 90 ? "…" : ""} <i>(${esc(s.keywords.join(", "))})</i></li>`).join("")}</ul>` : "");
  } catch (e) { el.textContent = e.message; }
}

// ------------------------------------------------------------------ settings
async function loadSettings() {
  const s = await api("/api/settings");
  state.settings = s;
  $("#s-enabled").checked = s.alerts_enabled;
  if (document.activeElement !== $("#s-recipients")) $("#s-recipients").value = s.recipients.join("\n");
  if (document.activeElement !== $("#s-url")) $("#s-url").value = s.app_url || "";
  const sessions = s.alert_sessions.map((x) => x.name).join(", ");
  $("#alerts-explainer").textContent =
    (s.filings_minutes > 0
      ? `Every ${s.filings_minutes < 60 ? s.filings_minutes + " minutes" : (s.filings_minutes === 60 ? "hour" : s.filings_minutes / 60 + " hours")} the tracker looks for bills newly filed in the ${sessions}, reads each one's full text, and checks it against the agenda keywords. `
      : `Automatic checks are turned off on this server; use Check for new filings on the Agenda page to look for bills newly filed in the ${sessions}. `) +
`Each matching bill gets its own email as soon as it's found. Bills that were already filed when the tracker started, or that only match after you edit keywords, are not emailed.`;
  const st = $("#email-status");
  if (s.email_configured) {
    st.className = "email-status ok";
    st.innerHTML = `Email is set up: messages are sent from <b>${esc(s.smtp_from)}</b> through <code>${esc(s.smtp_host)}</code>.`;
  } else {
    st.className = "email-status warn";
    st.innerHTML = `Email is not set up on the server yet, so alerts are queued but not sent. Whoever runs the tracker needs to set <code>SMTP_HOST</code>, <code>SMTP_USER</code>, <code>SMTP_PASSWORD</code> and <code>SMTP_FROM</code> (see the README).`;
  }
  const last = s.last_alert ? `Last alert sent ${ago(s.last_alert.at)} (${s.last_alert.bills} bill${s.last_alert.bills === 1 ? "" : "s"}).` : "No alerts sent yet.";
  const err = s.last_alert_error ? ` The last attempt failed ${ago(s.last_alert_error.at)}: ${s.last_alert_error.error}. Pending alerts will be retried at the next check.` : "";
  $("#s-last").textContent = last + err;

  $("#s-checkin").checked = s.checkin_enabled;
  $("#s-checkin-backup").checked = s.checkin_backup;
  $("#checkin-hour").textContent = s.checkin_hour > 12 ? s.checkin_hour - 12 : s.checkin_hour;
  $("#s-movement").checked = s.movement_enabled;
  $("#s-cats").innerHTML = s.action_categories.map((c) => `<label class="check" for="cat-${c.key}"><input type="checkbox" id="cat-${c.key}" data-cat="${c.key}" ${s.movement_categories.includes(c.key) ? "checked" : ""}> ${esc(c.label)}</label>`).join("");
  const every = s.refresh_hours <= 0 ? null : s.refresh_hours === 1 ? "hour" : s.refresh_hours < 1 ? `${Math.round(s.refresh_hours * 60)} minutes` : `${s.refresh_hours} hours`;
  $("#movement-hint").textContent = (every ? `Tracked bills are re-checked every ${every}. ` : "Automatic re-checks of tracked bills are turned off on this server. ") +
    "Each bill that moves gets one email listing its new actions.";
  $("#s-hearing").checked = s.hearing_email;
  $("#s-press").value = s.press_email;
  $("#s-redline").checked = s.redline_email;
  $("#s-report").checked = s.report_enabled;
  $("#report-hour").textContent = s.report_hour === 12 ? "noon" : s.report_hour > 12 ? `${s.report_hour - 12} p.m.` : `${s.report_hour} a.m.`;
  if (document.activeElement !== $("#s-report-to")) $("#s-report-to").value = (s.report_recipients || []).join("\n");
  renderIcal();
  renderInstallHelp(); renderPush(); loadSources();
  loadHealth();
  const watching = state.status ? state.status.alerts_watch : s.alert_sessions.some((x) => x.code === state.session);
  const on = s.alerts_enabled && s.email_configured && s.recipients.length;
  $("#alert-state").className = "alert-state" + (on ? "" : " off");
  $("#alert-state").innerHTML = !watching
    ? `Email alerts cover the ${esc(sessions)} only.`
    : on ? `Email alerts <b>on</b> for ${s.recipients.length} recipient${s.recipients.length === 1 ? "" : "s"}.`
      : `Email alerts <b>off</b>. <a href="#settings">Set up alerts</a>`;
}

async function loadHealth() {
  let h;
  try { h = await api("/api/health"); } catch { return; }
  state.health = h;
  const row = (ok, label, text) => `<div class="hrow"><span class="hdot ${ok === null ? "unk" : ok ? "ok" : "bad"}"></span><b>${label}</b><span>${text}</span></div>`;
  const checksBad = h.problems.some((p) => p.kind === "checks");
  $("#health").innerHTML = [
    row(h.checks.every_minutes ? !checksBad : null, "Checking for new bills",
        h.checks.every_minutes ? `Every ${h.checks.every_minutes} minutes · last successful check ${esc(h.checks.last_ok || "not yet")}` : "Automatic checks are off"),
    row(h.site.ok === undefined ? null : h.site.ok, "Reading oklegislature.gov",
        h.site.at ? (h.site.ok ? `Working · checked ${esc(h.site.at)}` : esc(h.site.detail || "Not reading correctly")) : "Not checked yet"),
    row(!h.email.configured ? false : !h.problems.some((p) => p.kind === "email"), "Email",
        !h.email.configured ? "Not set up on the server" : h.email.last_error && h.problems.some((p) => p.kind === "email")
          ? `Failing: ${esc(h.email.last_error)}` : `Working${h.email.last_sent ? ` · last alert ${esc(h.email.last_sent)}` : ""}`),
    row(h.checkin.at ? !!h.checkin.sent : null, "Daily check-in", h.checkin.at ? `Last sent ${esc(h.checkin.at)}` : `First one goes out at ${h.checkin.hour} a.m. Central`),
    row(h.backup.at ? true : null, "Backups", h.backup.at ? `Server copy ${esc(h.backup.at)} · 14 days kept` : "First server copy is made with the daily check-in"),
    row(h.calendar.at ? !h.calendar.errors.length : null, "Committee calendar", h.calendar.at ? `Every ${h.calendar.every_minutes} minutes · last ${esc(h.calendar.at)}${h.calendar.errors.length ? " · " + esc(h.calendar.errors[0]) : ""}` : "Not checked yet"),
    row(h.press.at ? !h.press.errors.length : null, "Press releases", h.press.at ? `Every ${h.press.every_minutes} minutes · last ${esc(h.press.at)}${h.press.errors.length ? " · " + esc(h.press.errors[0]) : ""}` : "Not checked yet"),
    row(h.push_devices ? true : null, "Phone notifications", h.push_devices ? `${h.push_devices} device${h.push_devices === 1 ? "" : "s"} signed up` : "No devices yet"),
  ].join("");
  renderBanner();
}

function renderBanner() {
  const h = state.health, b = $("#problem-banner");
  if (!h || !h.problems.length) { b.hidden = true; return; }
  b.hidden = false;
  b.innerHTML = h.problems.map((p) => `<div><b>${esc(p.title)}.</b> ${esc(p.message)}</div>`).join("") +
    `<div class="banner-fix">${esc(h.problems[0].fix)} <a href="#settings">Details</a></div>`;
}

async function saveSettings(e) {
  e.preventDefault();
  try {
    await api("/api/settings", { method: "PUT", body: {
      alerts_enabled: $("#s-enabled").checked,
      recipients: $("#s-recipients").value.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean),
      app_url: $("#s-url").value.trim(),
      checkin_enabled: $("#s-checkin").checked,
      checkin_backup: $("#s-checkin-backup").checked,
      movement_enabled: $("#s-movement").checked,
      hearing_email: $("#s-hearing").checked,
      press_email: $("#s-press").value,
      redline_email: $("#s-redline").checked,
      report_enabled: $("#s-report").checked,
      report_recipients: $("#s-report-to").value.split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean),
      movement_categories: [...document.querySelectorAll("#s-cats [data-cat]")].filter((x) => x.checked).map((x) => x.dataset.cat),
    } });
    toast("Settings saved");
    $("#s-recipients").blur(); $("#s-url").blur();
    loadSettings();
  } catch (err) { toast(err.message, true); }
}

async function testEmail() {
  $("#s-test").disabled = true;
  try {
    // Uses the addresses in the box right now, even before Save settings.
    const r = await api("/api/settings/test-email", { method: "POST", body: { to: $("#s-recipients").value } });
    toast(`Test email sent to ${r.to.join(", ")}.` + (r.unsaved.length ? " Click Save settings to send real alerts to the new address too." : ""));
  } catch (err) { toast(err.message, true); } finally { $("#s-test").disabled = false; }
}

function confirmReset() {
  $("#reset-area").innerHTML = `<span class="confirm">Replace all keyword edits with the defaults? <button class="btn btn-danger btn-sm" type="button" id="reset-yes">Restore defaults</button><button class="btn btn-ghost btn-sm" type="button" id="reset-no">Cancel</button></span>`;
  const restore = () => { $("#reset-area").innerHTML = `<button class="btn btn-ghost" type="button" id="agenda-reset">Restore default keywords</button>`; $("#agenda-reset").addEventListener("click", confirmReset); };
  $("#reset-no").addEventListener("click", restore);
  $("#reset-yes").addEventListener("click", async () => {
    await api("/api/agenda/reset", { method: "POST" });
    toast("Default keywords restored");
    restore();
    await Promise.all([loadAgenda(), loadTracked()]);
  });
}

// ------------------------------------------------------------------ my bills
async function loadTracked() {
  state.tracked = await api(`/api/tracked?session=${state.session}`);
  $("#count-mine").textContent = state.tracked.length ? `(${state.tracked.length})` : "";
  renderMine();
}

function renderPipeline() {
  const counts = {};
  state.tracked.forEach((t) => (counts[t.stage || "unknown"] = (counts[t.stage || "unknown"] || 0) + 1));
  const cards = [{ key: "", label: "All tracked", n: state.tracked.length }]
    .concat(state.config.stages.map((s) => ({ key: s.key, label: s.label, n: counts[s.key] || 0 })))
    .filter((c) => c.key === "" || c.n || STAGE_ORDER.includes(c.key));
  $("#pipeline").innerHTML = cards.map((c) =>
    `<button type="button" class="stage-card" data-stage="${c.key}" aria-pressed="${state.stageFilter === c.key}"><b>${c.n}</b><span>${esc(c.label)}</span></button>`).join("");
  $("#pipeline").querySelectorAll(".stage-card").forEach((b) => b.addEventListener("click", () => {
    state.stageFilter = state.stageFilter === b.dataset.stage ? "" : b.dataset.stage;
    renderMine();
  }));
}

function renderMine() {
  renderPipeline();
  const q = $("#mine-q").value.trim().toLowerCase();
  const pos = $("#mine-position").value, pri = $("#mine-priority").value, ag = $("#mine-agenda").value, sort = $("#mine-sort").value;
  let rows = state.tracked.filter((t) =>
    (!pos || t.position === pos) && (!pri || t.priority === pri) && (!state.stageFilter || t.stage === state.stageFilter) &&
    (!ag || (ag === "any" ? t.agenda.length : t.agenda.some((a) => a.id === ag))) &&
    (!q || [t.display, t.bill, t.title, t.labels, t.notes].join(" ").toLowerCase().includes(q)));
  if (sort === "recent") rows.sort((a, b) => (b.last_action_date || "").localeCompare(a.last_action_date || ""));
  if (sort === "priority") rows.sort((a, b) => PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority]);

  const closed = state.status && state.status.closed;
  $("#mine-table tbody").innerHTML = rows.map((t) => `
    <tr data-bill="${esc(t.bill)}">
      <td><span class="billno">${esc(t.display)}</span>${t.unseen ? `<span class="dot" title="${t.unseen} new action(s)"></span>` : ""}</td>
      <td class="author">${esc(t.author || "")}</td>
      <td class="title">${summaryCell(t)}${officialTitle(t)}
        ${agendaTags(t.agenda)}
        ${t.labels ? `<div class="labels">${t.labels.split(",").map((l) => l.trim()).filter(Boolean).map((l) => `<span class="tag">${esc(l)}</span>`).join("")}</div>` : ""}</td>
      <td class="last-action"><div>${esc(t.last_action || "")}</div><div class="date">${fmtDate(t.last_action_date)}</div></td>
      <td>${stagePill(t.stage, t.stage_label, closed)}</td>
      <td class="cell-pos"><select data-field="position" class="pos-${esc(t.position)}" aria-label="Position on ${esc(t.display)}">${optionList(state.config.positions, t.position)}</select>${t.reason ? `<div class="why-small" title="${esc(t.reason)}">${esc(t.reason)}</div>` : ""}</td>
      <td class="cell-pri"><select data-field="priority" aria-label="Priority for ${esc(t.display)}">${optionList(state.config.priorities, t.priority)}</select></td>
    </tr>`).join("");
  $("#mine-empty").hidden = state.tracked.length > 0;
  $("#mine-table").closest(".table-wrap").hidden = state.tracked.length === 0;

  bindSummarize($("#mine-table tbody"));
  $("#mine-table tbody").querySelectorAll("tr").forEach((tr) => {
    tr.addEventListener("click", (e) => { if (!e.target.closest("select, button")) openBill(state.session, tr.dataset.bill); });
    tr.querySelectorAll("select").forEach((sel) => sel.addEventListener("change", async () => {
      await saveTracking(tr.dataset.bill, { [sel.dataset.field]: sel.value });
    }));
  });
}

async function track(billText) {
  try {
    const row = await api("/api/tracked", { method: "POST", body: { session: state.session, bill: billText } });
    toast(`Now tracking ${row.display}`);
    await Promise.all([loadTracked(), loadAgenda()]);
    if (currentView() === "find") renderResults();
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

async function saveTracking(bill, fields) {
  try {
    const row = await api(`/api/tracked/${state.session}/${bill}`, { method: "PATCH", body: fields });
    const i = state.tracked.findIndex((t) => t.bill === bill);
    if (i >= 0) state.tracked[i] = row;
    renderMine();
    toast("Saved");
    return row;
  } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ new bills
async function runNewBills(reset) {
  if (reset) state.nb = { offset: 0, total: 0, results: [] };
  const p = new URLSearchParams({ session: state.session, q: $("#nb-q").value.trim(), priority: $("#nb-priority").value,
    review: $("#nb-review").value, position: $("#nb-position").value, offset: state.nb.offset, limit: 30 });
  $("#nb-meta").textContent = "Loading…";
  try {
    const data = await api(`/api/newbills?${p}`);
    state.nb.total = data.total;
    state.nb.results = state.nb.results.concat(data.results);
    state.nb.offset = state.nb.results.length;
    renderNewBills();
  } catch (e) { $("#nb-meta").textContent = e.message; }
}

function renderNewBills() {
  const { results, total } = state.nb;
  const q = $("#nb-q").value.trim();
  $("#nb-meta").textContent = results.length
    ? `Showing ${results.length.toLocaleString()} of ${total.toLocaleString()} bills that match the agenda, newest first${q ? ` · containing “${q}”` : ""}`
    : "";
  $("#nb-empty").hidden = results.length > 0;
  if (!results.length && q) { $("#nb-empty").hidden = true; $("#nb-meta").textContent = `No agenda bills contain “${q}”. Try Find bills to search every bill.`; }
  const pid = $("#nb-priority").value;
  $("#nb-list").innerHTML = results.map((r) => {
    const ags = (r.agenda || []).filter((a) => !pid || a.id === pid);
    return `
    <li class="nb-card">
      <div class="nb-head">
        <a class="billno nb-open" href="#" data-bill="${esc(r.bill)}">${esc(r.display)}</a>
        ${r.author ? `<span class="by">by ${esc(r.author)}</span>` : ""}
        <span class="date">${r.first_seen ? "Found " + fmtDate(r.first_seen) : ""}</span>
        ${r.emailed_at ? `<span class="badge-mail" title="Alert emailed ${esc(fmtDate(r.emailed_at))}">Emailed</span>` : ""}
      </div>
      <div class="nb-areas">${ags.map((a) => {
        const p = priorityById(a.id);
        return p ? `<div class="nb-area"><span><b>${esc(shortLabel(p))}</b> · ${esc(p.title)}</span>${reviewButtons(r.bill, a.id, a.review)}</div>` : "";
      }).join("")}</div>
      ${summaryCell(r)}
      ${snippetHtml(r.snippet)}
      ${miniProgress(r.progress, r)}
      <div class="nb-foot">
        ${positionPicker(r)}
        <div class="nb-links">
          ${r.text_url ? `<a href="${esc(r.text_url)}" target="_blank" rel="noopener">Bill text (PDF${r.text_version ? ", " + esc(r.text_version) : ""})</a>` : ""}
          <a href="${esc(r.url)}" target="_blank" rel="noopener">oklegislature.gov page</a>
          <a href="#" class="nb-open" data-bill="${esc(r.bill)}">Full details</a>
        </div>
      </div>
      ${officialTitle(r)}
    </li>`;
  }).join("");
  $("#nb-more").hidden = results.length >= total;
  bindSummarize($("#nb-list"));
  bindPositions($("#nb-list"), state.nb.results);
  bindReviews($("#nb-list"), (bill, rpid, verdict) => {
    const view = $("#nb-review").value;
    const hide = (view === "" && verdict === "not_relevant") || (view === "todo" && verdict) ||
                 (view === "relevant" && verdict !== "relevant") || (view === "not_relevant" && verdict !== "not_relevant");
    const r = state.nb.results.find((x) => x.bill === bill);
    const allDismissed = r && (r.agenda || []).every((a) => a.review === "not_relevant");
    if (hide && (view !== "" || allDismissed)) {
      state.nb.results = state.nb.results.filter((x) => x.bill !== bill);
      state.nb.total = Math.max(0, state.nb.total - 1);
      renderNewBills();
    }
  });
  $("#nb-list").querySelectorAll(".nb-open").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openBill(state.session, a.dataset.bill); }));
}

// ------------------------------------------------------------------ find bills
async function loadSubjects() {
  const subjects = await api(`/api/subjects?session=${state.session}`);
  const cur = $("#find-subject").value;
  $("#find-subject").innerHTML = `<option value="">All subjects</option>` +
    subjects.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} (${s.count})</option>`).join("");
  $("#find-subject").value = subjects.some((s) => s.name === cur) ? cur : "";
}

async function runSearch(reset) {
  if (reset) state.find = { offset: 0, total: 0, results: [] };
  const p = new URLSearchParams({
    session: state.session, q: $("#find-q").value.trim(), subject: $("#find-subject").value, agenda: $("#find-agenda").value,
    dismissed: $("#find-dismissed").value,
    chamber: $("#find-chamber").value, stage: $("#find-stage").value, sort: $("#find-sort").value, offset: state.find.offset, limit: 50,
  });
  $("#find-meta").textContent = "Searching…";
  try {
    const data = await api(`/api/search?${p}`);
    state.find.total = data.total;
    state.find.results = state.find.results.concat(data.results);
    state.find.offset = state.find.results.length;
    renderResults();
  } catch (e) {
    $("#find-meta").textContent = e.message;
  }
}

function renderResults() {
  const { results, total } = state.find;
  const noIndex = !state.status || (!state.status.indexed && !state.status.bills);
  const q = $("#find-q").value.trim();
  $("#find-noindex").hidden = !(noIndex && !results.length);
  const ag = $("#find-agenda").value, p = ag && ag !== "any" ? priorityById(ag) : null;
  $("#find-dismissed").hidden = !ag;
  $("#find-meta").textContent = results.length
    ? `Showing ${results.length.toLocaleString()} of ${total.toLocaleString()} measures${p ? ` matching ${shortLabel(p)}: ${p.title}` : ag === "any" ? " matching the agenda" : ""}`
    : noIndex ? "" : q ? `No measures match “${q}”.` : "No measures match these filters.";
  const tracked = new Set(state.tracked.map((t) => t.bill));
  const closed = state.status && state.status.closed;
  $("#find-results").innerHTML = results.map((r) => `
    <li class="result">
      <div class="result-main" data-bill="${esc(r.bill)}">
        <div class="result-top"><span class="billno">${esc(r.display)}</span>${r.author ? `<span class="by">by ${esc(r.author)}</span>` : ""}${stagePill(r.stage, r.stage_label, closed)}</div>
        ${summaryCell(r)}
        ${snippetHtml(r.snippet)}
        <p class="last"><b>Last action:</b> ${esc(r.last_action || "No action recorded")}${r.last_action_date ? " · " + fmtDate(r.last_action_date) : ""}</p>
        ${officialTitle(r)}
        ${agendaTags(r.agenda)}
        ${p ? (() => { const a = (r.agenda || []).find((x) => x.id === p.id); return a ? `<div class="review-row"><span class="matched-on">Matched on: ${esc(a.keywords.slice(0, 4).join(", "))}</span>${reviewButtons(r.bill, p.id, a.review)}</div>` : ""; })() : ""}
      </div>
      <div class="result-actions">${tracked.has(r.bill)
        ? `<span class="tracked-flag">Tracking</span>`
        : `<button class="btn btn-sm" type="button" data-track="${esc(r.bill)}">Track</button>`}</div>
    </li>`).join("");
  $("#find-more").hidden = results.length >= total;
  $("#find-results").querySelectorAll("[data-track]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "Adding…";
    if (!(await track(b.dataset.track))) { b.disabled = false; b.textContent = "Track"; }
  }));
  bindSummarize($("#find-results"));
  bindReviews($("#find-results"), (bill, pid, verdict) => {
    const view = $("#find-dismissed").value;
    const gone = (view === "" && verdict === "not_relevant") || (view === "only" && verdict !== "not_relevant");
    if (gone) {
      state.find.results = state.find.results.filter((r) => r.bill !== bill);
      state.find.total = Math.max(0, state.find.total - 1);
      renderResults();
      toast(verdict === "not_relevant" ? "Marked not relevant. It’s listed under “Marked not relevant” if you change your mind." : "Moved back to the review list.");
    }
  });
  $("#find-results").querySelectorAll(".result-main").forEach((el) => el.addEventListener("click", (e) => { if (!e.target.closest("button")) openBill(state.session, el.dataset.bill); }));
}

// ------------------------------------------------------------------ updates
async function loadUpdates() {
  state.updates = await api(`/api/updates?session=${state.session}`);
  const unseen = state.updates.filter((u) => !u.seen).length;
  $("#count-updates").textContent = unseen;
  $("#count-updates").hidden = !unseen;
  $("#feed-empty").hidden = state.updates.length > 0;
  $("#feed").hidden = !state.updates.length;
  $("#feed").innerHTML = state.updates.map((u) => `
    <li class="${u.seen ? "" : "unseen"}" data-bill="${esc(u.bill)}">
      <span class="date">${fmtDate(u.date)}</span>
      <div class="what"><span class="billno">${esc(u.display)}</span><span class="chamber">${u.chamber === "H" ? "House" : u.chamber === "S" ? "Senate" : esc(u.chamber)}</span>
        <p>${esc(u.action)}</p><p class="sub">${esc(u.title || "")}</p></div>
    </li>`).join("");
  $("#feed").querySelectorAll("li").forEach((li) => li.addEventListener("click", () => openBill(state.session, li.dataset.bill)));
}

// ------------------------------------------------------------------ drawer
function closeDrawer() {
  $("#drawer").hidden = true; $("#scrim").hidden = true; state.drawer = null;
}

async function openBill(session, bill, refresh = false) {
  $("#drawer").hidden = false; $("#scrim").hidden = false;
  if (!refresh) $("#drawer-body").innerHTML = `<p class="skeleton">Loading ${esc(bill)} from oklegislature.gov…</p>`;
  $("#drawer-refresh").disabled = true;
  try {
    const d = await api(`/api/bill/${session}/${bill}${refresh ? "?refresh=1" : ""}`);
    state.drawer = { session, bill, data: d };
    renderDrawer(d);
    const row = state.tracked.find((t) => t.bill === bill);
    if (row && row.unseen) {
      await api(`/api/updates/seen?session=${session}`, { method: "POST", body: { bill } });
      loadUpdates(); loadTracked();
    }
    if (refresh) toast("Reloaded from oklegislature.gov");
  } catch (e) {
    $("#drawer-body").innerHTML = `<p class="skeleton">${esc(e.message)}</p>`;
  } finally {
    $("#drawer-refresh").disabled = false;
  }
}

function aiBlock(d) {
  const ai = d.ai;
  if (ai && ai.summary) {
    const m = ai.model || "";
    const note = m.startsWith("claude")
      ? `Written by Claude from the ${esc(ai.version || "latest")} text${ai.truncated ? " (long bill: summarized from the first part)" : ""}. AI summaries can be wrong; check the bill.`
      : m === "official" ? `Written by legislative staff: ${esc(ai.version || "")}. Updated when a newer staff summary is posted.`
      : `Taken from ${esc(ai.version || "the bill's official description")}. The tracker switches to the staff summary when one is posted.`;
    return `<h3 class="section-h">${esc(summarySource(m) || "Summary")}</h3><p class="ai-summary big">${esc(ai.summary)}</p>
      <p class="ai-note">${note} ${ai.source_url ? `<a href="${esc(ai.source_url)}" target="_blank" rel="noopener">Source</a> ·` : ""}
      <button class="linkish" type="button" id="ai-redo">Check for an update</button></p>`;
  }
  return `<h3 class="section-h">Summary</h3><p class="ai-missing">${ai && ai.error ? esc(ai.error) + " " : ""}<button class="btn btn-sm" type="button" id="ai-redo">Get summary</button></p>`;
}
function bindAi(d) {
  const b = $("#ai-redo");
  if (!b) return;
  b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "Checking oklegislature.gov…";
    try {
      d.ai = await api(`/api/bill/${d.session}/${d.bill}/summary`, { method: "POST" });
      [state.tracked, state.find.results].forEach((list) => list.forEach((r) => { if (r.bill === d.bill) { r.summary = d.ai.summary; r.summary_model = d.ai.model; } }));
      $("#d-ai").innerHTML = aiBlock(d); bindAi(d);
      renderMine(); if (currentView() === "find") renderResults();
    } catch (err) { toast(err.message, true); b.disabled = false; b.textContent = "Try again"; }
  });
}

const KEY_ACTION = /passed|approved by governor|vetoed|sent to governor|do pass|engrossed|enrolled|became law|failed/i;

function renderDrawer(d) {
  const reach = STAGE_ORDER.indexOf(d.stage === "vetoed" || d.stage === "failed" ? "governor" : d.stage);
  const t = d.tracked;
  const closed = state.status && state.status.closed && d.session === state.session;
  const unseenKeys = new Set(state.updates.filter((u) => u.bill === d.bill && !u.seen).map((u) => u.action + u.date));

  const groups = {};
  d.documents.forEach((doc) => (groups[doc.group] = groups[doc.group] || []).push(doc));
  const authorLink = (name, key) => !name ? "" : key ? `<a href="#legislator-${esc(key.replace(":", "-"))}">${esc(name)}</a>` : esc(name);
  const authorsHtml = [authorLink(d.author, d.author_key), authorLink(d.other_author, d.other_author_key)].filter(Boolean).join(" and ");
  const authors = [d.author, d.other_author].filter(Boolean).join(" and ");
  const agendaBlock = (d.agenda || []).map((a) => {
    const p = priorityById(a.id);
    return p ? `<div class="d-agenda-row"><div><b>${esc(p.task_force)} #${p.number}:</b> ${esc(p.title)} <span class="kw">(matched: ${esc(a.keywords.join(", "))})</span></div>${reviewButtons(d.bill, p.id, a.review)}</div>` : "";
  }).join("");

  $("#drawer-body").innerHTML = `
    <div class="d-title">
      <span class="billno">${esc(d.display)}</span> ${stagePill(d.stage, d.stage_label, closed)}
      <h2>${esc(d.title || "Title not yet available")}</h2>
      <div class="d-meta">
        ${authors ? `<span>By ${authorsHtml}</span>` : ""}
        ${d.coauthors.length ? `<span>Coauthors: ${esc(d.coauthors.join(", "))}</span>` : ""}
        ${d.subjects.length ? `<span>Subjects: ${esc(d.subjects.join(", "))}</span>` : ""}
        <span>Fetched ${ago(d.fetched_at)}${d.stale ? " (site unreachable, showing saved copy)" : ""}</span>
      </div>
    </div>

    <div class="d-ai" id="d-ai">${aiBlock(d)}</div>

    ${agendaBlock ? `<div class="d-agenda"><h3 class="section-h" style="margin:0">OneVoice agenda</h3>${agendaBlock}</div>` : ""}

    ${d.progress ? `<div class="prog">
      <div class="prog-head"><span class="prog-pct st-${esc(d.progress.status)}">${d.progress.percent}%</span><span>${esc(d.progress.where)}</span></div>
      <div class="prog-bar st-${esc(d.progress.status)}">${d.progress.steps.map((st) => `<i class="${st.done ? "on" : st.current ? "cur" : ""}" title="${esc(st.label)}"></i>`).join("")}</div>
      <div class="prog-steps">${d.progress.steps.map((st) => `<span class="${st.done ? "on" : st.current ? "cur" : ""}">${st.done ? "✓ " : ""}${esc(st.label)}</span>`).join(" › ")}</div>
    </div>` : ""}

    ${versionsBlock(d)}

    <div class="d-links">
      <a class="btn btn-ghost btn-sm" href="${esc(d.url)}" target="_blank" rel="noopener">Open on oklegislature.gov</a>
      ${d.introduced_url ? `<a class="btn btn-ghost btn-sm" href="${esc(d.introduced_url)}" target="_blank" rel="noopener">Introduced text (PDF)</a>` : ""}
      <a class="btn btn-ghost btn-sm" href="${esc(d.lens_url)}" target="_blank" rel="noopener">Email alerts from the Legislature (LENS)</a>
    </div>

    ${t ? `
    <form class="track-box" id="track-form">
      <h3 class="section-h">Your tracking</h3>
      <div class="track-grid">
        <label for="t-position">Position<select id="t-position">${optionList(state.config.positions, t.position)}</select></label>
        <label for="t-priority">Priority<select id="t-priority">${optionList(state.config.priorities, t.priority)}</select></label>
        <label for="t-labels">Labels (comma separated)<input id="t-labels" value="${esc(t.labels)}" placeholder="e.g. education, budget"></label>
        <label for="t-track">Track on the Friday report<input id="t-track" list="track-options" value="${esc(t.track || "")}" placeholder="Automatic (OneVoice task force)">
          <datalist id="track-options">${(d.track_options || []).map((o) => `<option value="${esc(o)}">`).join("")}</datalist></label>
      </div>
      <label for="t-reason">Why this position<input id="t-reason" maxlength="1000" value="${esc(t.reason || "")}" placeholder="The reason for the Chamber’s position, for the record"></label>
      <label for="t-notes">Notes<textarea id="t-notes" placeholder="Talking points, contacts, next steps">${esc(t.notes)}</textarea></label>
      <div class="row-end">
        <button class="btn" type="submit">Save</button>
        <span id="untrack-area"><button class="btn btn-danger btn-sm" type="button" id="untrack">Stop tracking</button></span>
      </div>
    </form>` : `
    <div class="track-box row-end">
      <span>You are not tracking this bill.</span>
      <button class="btn" type="button" id="track-this">Track this bill</button>
    </div>`}

    <div>
      <h3 class="section-h">History (${d.actions.length} actions, newest first)</h3>
      ${d.actions.length ? `<ol class="timeline">${d.actions.slice().reverse().map((a) => `
        <li class="${unseenKeys.has(a.action + a.date) ? "new" : KEY_ACTION.test(a.action) ? "key" : ""}">
          <div class="when">${fmtDate(a.date)}<span class="chamber">${a.chamber === "H" ? "House" : a.chamber === "S" ? "Senate" : esc(a.chamber)}</span>${a.journal_page ? ` · Journal p. ${esc(a.journal_page)}` : ""}</div>
          <div>${esc(a.action)}</div>
        </li>`).join("")}</ol>` : `<p class="hint">No actions recorded yet.</p>`}
    </div>

    <div>
      <h3 class="section-h">Documents</h3>
      ${Object.keys(groups).length ? `<div class="docs">${Object.entries(groups).map(([g, docs]) => `
        <div><h4>${esc(g)}</h4><ul>${docs.map((doc) => `
          <li><a href="${esc(doc.url)}" target="_blank" rel="noopener">${esc(doc.name)}</a><span class="date">${fmtDate(doc.date)}</span></li>`).join("")}</ul></div>`).join("")}</div>`
        : `<p class="hint">No versions, amendments, summaries or votes posted yet.</p>`}
    </div>`;

  bindAi(d);
  bindVersions(d);
  bindReviews($("#drawer-body"), (bill, pid, verdict) => { (d.agenda || []).forEach((a) => { if (a.id === pid) a.review = verdict; }); if (currentView() === "find") runSearch(true); });
  if (t) {
    $("#t-position").addEventListener("change", (e) => saveTracking(d.bill, { position: e.target.value }));
    $("#t-priority").addEventListener("change", (e) => saveTracking(d.bill, { priority: e.target.value }));
    $("#track-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const row = await saveTracking(d.bill, { notes: $("#t-notes").value, labels: $("#t-labels").value, reason: $("#t-reason").value, track: $("#t-track").value });
      if (row) d.tracked = { ...d.tracked, notes: row.notes, labels: row.labels, reason: row.reason, track: row.track };
    });
    $("#untrack").addEventListener("click", () => {
      $("#untrack-area").innerHTML = `<span class="confirm">Remove ${esc(d.display)} and its notes? <button class="btn btn-danger btn-sm" type="button" id="untrack-yes">Remove</button><button class="btn btn-ghost btn-sm" type="button" id="untrack-no">Cancel</button></span>`;
      $("#untrack-no").addEventListener("click", () => renderDrawer(d));
      $("#untrack-yes").addEventListener("click", async () => {
        await api(`/api/tracked/${d.session}/${d.bill}`, { method: "DELETE" });
        toast(`Stopped tracking ${d.display}`);
        d.tracked = null;
        await Promise.all([loadTracked(), loadUpdates(), loadAgenda()]);
        if (currentView() === "find") renderResults();
        renderDrawer(d);
      });
    });
  } else {
    $("#track-this").addEventListener("click", async () => {
      if (await track(d.bill)) openBill(d.session, d.bill);
    });
  }
}

// ------------------------------------------------------------------ chart helpers
// Charts are plain HTML bars: thin marks, value labels in text colors, a hover
// tooltip on every mark, and a table view of the same numbers.
const POS_ORDER = ["support", "neutral", "monitor", "oppose"];
let tipEl;
function tip(e, html) {
  if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "chart-tip"; tipEl.setAttribute("role", "tooltip"); document.body.appendChild(tipEl); }
  if (!html) { tipEl.hidden = true; return; }
  clearTimeout(tipTimer);
  tipEl.innerHTML = html; tipEl.hidden = false;
  const r = tipEl.getBoundingClientRect();
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - 14;
  if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - 14;
  tipEl.style.left = `${Math.max(8, x)}px`; tipEl.style.top = `${Math.max(8, y)}px`;
}
// Tooltips follow a mouse, or keyboard focus. On a phone a tap shows the tip for a
// moment and any scroll, tap or screen change hides it, so it can never get stuck.
let tipTimer;
function hideTip() { clearTimeout(tipTimer); if (tipEl) tipEl.hidden = true; }
["scroll", "hashchange", "resize"].forEach((ev) => window.addEventListener(ev, hideTip, { passive: true }));
document.addEventListener("pointerdown", (e) => { if (!e.target.closest || !e.target.closest("[data-tip]")) hideTip(); }, { passive: true });
function bindTips(root) {
  root.querySelectorAll("[data-tip]").forEach((el) => {
    el.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse") tip(e, el.dataset.tip); });
    el.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") hideTip(); });
    el.addEventListener("pointerup", (e) => {
      if (e.pointerType === "mouse") return;
      tip(e, el.dataset.tip); clearTimeout(tipTimer); tipTimer = setTimeout(hideTip, 2500);
    });
    el.addEventListener("focus", () => {
      if (!el.matches(":focus-visible")) return;  // keyboard only; taps are handled above
      const r = el.getBoundingClientRect(); tip({ clientX: r.left, clientY: r.bottom }, el.dataset.tip);
    });
    el.addEventListener("blur", hideTip);
  });
}
function barChart(rows, { total, unit = "bills", href } = {}) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return `<div class="hbars">${rows.map((r) => {
    const pct = total ? ` (${Math.round((100 * r.value) / Math.max(1, total))}%)` : "";
    const t = esc(`<b>${esc(r.label)}</b><br>${r.value.toLocaleString()} ${unit}${pct}`);
    const inner = `<span class="hb-label">${esc(r.label)}</span>
      <span class="hb-track"><span class="hb-bar" style="width:${(100 * r.value) / max}%"></span><span class="hb-val">${r.value.toLocaleString()}${pct}</span></span>`;
    return r.href ? `<a class="hb-row" href="${esc(r.href)}" data-tip="${t}">${inner}</a>` : `<div class="hb-row" tabindex="0" data-tip="${t}">${inner}</div>`;
  }).join("")}</div>`;
}
function dataTable(head, rows) {
  return `<table class="data"><thead><tr>${head.map((h, i) => `<th${i ? ' class="num"' : ""}>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td${i ? ' class="num"' : ""}>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

// ------------------------------------------------------------------ scoreboard
let sbTables = false;
async function loadScoreboard() {
  let d;
  try { d = await api(`/api/scoreboard?session=${state.session}`); } catch (e) { $("#sb-kpis").innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  const k = d.kpis;
  $("#sb-title").textContent = `${d.session_name} scoreboard`;
  $("#sb-sub").textContent = d.closed ? "Final results for the session." : "Updates as bills move. Positions come from your tracking list.";
  const kpi = (v, label, sub, href) => `<a class="kpi" href="${href}"><b>${v}</b><span>${esc(label)}</span>${sub ? `<small>${esc(sub)}</small>` : ""}</a>`;
  $("#sb-kpis").innerHTML = [
    kpi(k.onevoice.toLocaleString(), "OneVoice bills filed", "match the agenda", "#newbills"),
    kpi(k.relevant.toLocaleString(), "Marked Relevant", "by your team", "#newbills"),
    kpi(k.tracked.toLocaleString(), "Tracked", `${k.with_position} with a position other than Monitor`, "#mine"),
    kpi(k.enacted.toLocaleString(), "OneVoice bills enacted", "signed into law", "#scoreboard"),
    kpi(k.win_rate == null ? "–" : `${k.win_rate}%`, "Supported bills enacted", k.supported ? `${k.supported_enacted} of ${k.supported} supported` : "No Support positions yet", "#mine"),
    kpi(d.wins.length.toLocaleString(), "Wins", "supported and passed, or opposed and stopped", "#scoreboard"),
  ].join("");
  const tf = d.by_task_force.map((r) => ({ label: r.task_force, value: r.count, href: "#newbills" }));
  const funnel = d.funnel.map((r) => ({ label: r.label, value: r.count }));
  const posTotal = d.positions.reduce((a, p) => a + p.count, 0);
  const posLegend = `<div class="legend">${POS_ORDER.map((p) => { const n = (d.positions.find((x) => x.position === p) || {}).count || 0;
    return `<span><i class="sw sw-${p}"></i>${cap(p)} <b>${n}</b></span>`; }).join("")}</div>`;
  const posBar = posTotal ? `<div class="stack-bar">${POS_ORDER.map((p) => { const n = (d.positions.find((x) => x.position === p) || {}).count || 0;
    return n ? `<span class="seg-${p}" style="flex:${n}" tabindex="0" data-tip="${esc(`<b>${cap(p)}</b><br>${n} bill${n === 1 ? "" : "s"} (${Math.round((100 * n) / posTotal)}%)`)}"></span>` : ""; }).join("")}</div>` : `<p class="hint">Choose a position on tracked bills and it shows up here.</p>`;
  const card = (title, sub, chart, table) => `<figure class="chart-card"><figcaption><b>${esc(title)}</b><span class="hint">${esc(sub)}</span></figcaption>${sbTables ? table : chart}</figure>`;
  $("#sb-charts").innerHTML = [
    card("OneVoice bills by task force", "Bills matching each task force’s priorities (a bill can match more than one)", barChart(tf),
      dataTable(["Task force", "Bills"], tf.map((r) => [r.label, r.value]))),
    card("How far OneVoice bills got", "Bills that reached each step (share of all OneVoice bills)", barChart(funnel, { total: k.onevoice }),
      dataTable(["Step reached", "Bills", "Share"], funnel.map((r) => [r.label, r.value, `${Math.round((100 * r.value) / Math.max(1, k.onevoice))}%`]))),
    card("Chamber positions", `${posTotal} tracked bill${posTotal === 1 ? "" : "s"}`, posLegend + posBar,
      dataTable(["Position", "Bills"], POS_ORDER.map((p) => [cap(p), (d.positions.find((x) => x.position === p) || {}).count || 0]))),
  ].join("");
  const li = (w) => `<li><button class="linklike" type="button" data-open-bill="${esc(w.bill)}"><b>${esc(w.display)}</b></button> ${esc(w.title)} <span class="hint">· ${esc(w.why)}</span></li>`;
  $("#sb-wins").innerHTML = `<h2>Wins</h2>${d.wins.length ? `<ol class="win-list">${d.wins.map(li).join("")}</ol>` : `<p class="hint">Bills you supported that become law, and bills you opposed that are stopped, are listed here.</p>`}
    ${d.losses.length ? `<h3 class="sub-h">Didn’t go our way</h3><ol class="win-list">${d.losses.map(li).join("")}</ol>` : ""}`;
  bindTips($("#sb-charts"));
  document.querySelectorAll("#sb-wins [data-open-bill]").forEach((b) => b.addEventListener("click", () => openBill(state.session, b.dataset.openBill)));
}

// ------------------------------------------------------------------ goals
const G = { year: null, data: null, tab: "score" };
const STATUS_TXT = { met: "✓ Goal met", on_track: "● On pace", behind: "▲ Behind pace", upcoming: "Starts Jan. 1", open: "In progress", info: "" };
function yearOptions(sel, chosen) {
  const now = new Date().getFullYear();
  const years = [...new Set([now - 1, now, now + 1, 2027, chosen])].filter(Boolean).sort();
  $(sel).innerHTML = years.map((y) => `<option ${y === chosen ? "selected" : ""}>${y}</option>`).join("");
}
function meter(m) {
  if (!m.target) return "";
  const pct = Math.min(100, (100 * (m.value || 0)) / m.target);
  const exp = m.expected != null && m.status !== "met" ? Math.min(100, (100 * m.expected) / m.target) : null;
  return `<div class="meter" role="img" aria-label="${esc(`${m.value ?? 0} of ${m.target}`)}"><span class="meter-fill st-${m.status}" style="width:${pct}%"></span>
    ${exp != null && exp > 0 ? `<span class="meter-pace" style="left:${exp}%" title="Where we should be by today: ${m.expected}"></span>` : ""}</div>`;
}
function metricTip(m) {
  return esc(`<b>${esc(m.label)}</b><br>${m.value ?? "–"}${m.unit} of ${m.target ?? "–"}${m.unit}` +
    (m.expected != null && m.status !== "met" && m.status !== "upcoming" ? `<br>Pace for today: ${m.expected}${m.unit}` : "") + (m.note ? `<br>${esc(m.note)}` : ""));
}
async function loadGoals() {
  try { G.data = await api(`/api/goals${G.year ? `?year=${G.year}` : ""}`); G.year = G.data.year; } catch (e) { $("#goals-score").innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  yearOptions("#goals-year", G.year);
  $("#goals-export").href = `/api/goals/export.xlsx?year=${G.year}`;
  $("#goals-title").textContent = `${G.year} Government Affairs goals`;
  $("#goals-sub").textContent = `Owner: ${G.data.owner}. Bill numbers fill in on their own; log meetings, donors and activities as they happen.`;
  renderGoalTabs();
}
function renderGoalTabs() {
  document.querySelectorAll("#goals-tabs [data-gtab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.gtab === G.tab)));
  ["score", "log", "milestones"].forEach((t) => ($(`#goals-${t}`).hidden = t !== G.tab));
  if (G.tab === "score") renderGoalScore();
  if (G.tab === "log") renderGoalLog();
  if (G.tab === "milestones") renderMilestones();
}
function renderGoalScore() {
  const d = G.data;
  const head = d.metrics.filter((m) => m.headline);
  const big = head.map((m) => `<div class="goal-kpi" tabindex="0" data-tip="${metricTip(m)}">
      <span class="gk-label">${esc(m.label)}</span>
      <b>${m.value ?? "–"}${esc(m.unit)}<small> / ${m.target}${esc(m.unit)}</small></b>
      ${meter(m)}<span class="st-chip st-${m.status}">${STATUS_TXT[m.status]}</span></div>`).join("");
  const goalSec = d.goals.map((g) => `<section class="panel wide goal-sec"><h2>Goal ${g.n}: ${esc(g.title)}</h2><p class="hint">${esc(g.text)}</p>
      <ul class="metric-list">${d.metrics.filter((m) => m.goal === g.n).map((m) => `<li tabindex="0" data-tip="${metricTip(m)}">
        <span class="ml-label">${esc(m.label)}${m.note ? `<small>${esc(m.note)}</small>` : ""}</span>
        <span class="ml-val">${m.value ?? "–"}${esc(m.unit)}${m.target ? ` <small>of ${m.target}${esc(m.unit)}</small>` : ""}</span>
        ${meter(m) || "<span></span>"}<span class="st-chip st-${m.status}">${STATUS_TXT[m.status]}</span></li>`).join("")}</ul>
      ${g.n === 1 ? goalBills() : ""}${g.n === 2 ? `<a class="btn btn-ghost btn-sm" href="#meetings">Log a meeting</a>` : ""}
      ${g.n === 3 ? `<button class="btn btn-ghost btn-sm" type="button" data-gtab-go="log">Log a donor or PAC activity</button>` : ""}</section>`).join("");
  const t = d.targets;
  const editable = [["bills_passed", "OneVoice bills passed"], ["ballot_designated", "Designated ballot initiatives"], ["contacts", "Face-to-face contacts"],
    ["contacts_local", "Municipal/county contacts"], ["contacts_state", "State contacts"], ["contacts_federal", "Federal contacts"], ["distinct", "Distinct leaders"],
    ["roundtable", "Briefings/roundtables"], ["coalition", "Coalition updates"], ["federal", "Federal initiatives"], ["quarterly_report", "Quarterly reports"],
    ["donors", "PAC donors"], ["pac_board", "Board meetings per PAC"], ["attendance", "Board attendance %"], ["forum", "PAC forums"]];
  $("#goals-score").innerHTML = `<div class="goal-kpis">${big}</div>
    <p class="hint legend-line"><span class="pace-key"></span> marks where each number should be by today, based on the quarterly targets (or an even pace through the year).</p>
    ${goalSec}
    <details class="panel wide"><summary><b>Edit targets</b></summary>
      <form id="targets-form" class="targets">${editable.map(([k, l]) => `<label>${esc(l)}<input type="number" min="0" name="${k}" value="${t[k]}"></label>`).join("")}
      <div class="row-end"><button class="btn btn-sm" type="submit">Save targets</button></div></form></details>`;
  bindTips($("#goals-score"));
  document.querySelectorAll("[data-gtab-go]").forEach((b) => b.addEventListener("click", () => { G.tab = b.dataset.gtabGo; renderGoalTabs(); }));
  document.querySelectorAll("[data-gbill]").forEach((b) => b.addEventListener("click", async () => {
    try { G.data = await api("/api/goals/bill", { method: "POST", body: { year: G.year, bill: b.dataset.gbill, count: b.dataset.count === "null" ? null : b.dataset.count === "true" } }); renderGoalScore(); }
    catch (e) { toast(e.message, true); }
  }));
  const addForm = $("#gbill-add");
  if (addForm) addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { G.data = await api("/api/goals/bill", { method: "POST", body: { year: G.year, bill: $("#gbill-input").value, count: true } }); renderGoalScore(); toast("Bill counted"); }
    catch (err) { toast(err.message, true); }
  });
  $("#targets-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = Object.fromEntries([...new FormData(e.target)].map(([k, v]) => [k, Number(v)]));
    try { G.data = await api(`/api/goals/targets?year=${G.year}`, { method: "PUT", body }); toast("Targets saved"); renderGoalScore(); }
    catch (err) { toast(err.message, true); }
  });
}
function goalBills() {
  const b = G.data.bills;
  return `<details class="gbills"><summary>Bills counted toward “bills passed” (${b.length})</summary>
    <p class="hint">Counted automatically: enacted bills that match the agenda and that you supported or marked Relevant. Add or remove any bill by hand.</p>
    <ul>${b.map((x) => `<li><b>${esc(x.display)}</b> ${esc(x.title)} ${x.added ? '<span class="hint">(added by hand)</span>' : ""}
      <button class="btn btn-ghost btn-sm" type="button" data-gbill="${esc(x.bill)}" data-count="${x.added ? "null" : "false"}">Don’t count</button></li>`).join("") || '<li class="hint">None yet.</li>'}</ul>
    <form class="toolbar" id="gbill-add"><input id="gbill-input" placeholder="Bill number, e.g. HB 1234" aria-label="Bill to count"><button class="btn btn-sm" type="submit">Count this bill</button></form></details>`;
}
function todayIso() { return isoDate(new Date()); }
function entryForm(kind) {
  const d = G.data;
  if (kind === "donor") return `<form class="panel" id="donor-form"><h2>Log a PAC donor</h2>
    <div class="form-grid"><label>Date<input type="date" name="date" value="${todayIso()}" required></label>
    <label>Donor<input name="name" required placeholder="Name or company"></label>
    <label>PAC<select name="pac">${d.pacs.map((p) => `<option>${esc(p)}</option>`).join("")}</select></label>
    <label>New or reactivated<select name="status"><option value="new">New</option><option value="reactivated">Reactivated</option></select></label>
    <label>Amount (optional)<input name="amount" inputmode="decimal" placeholder="$"></label></div>
    <label class="stack">Notes<input name="notes"></label><div class="row-end"><button class="btn" type="submit">Save donor</button></div></form>`;
  return `<form class="panel" id="activity-form"><h2>Log an activity</h2>
    <div class="form-grid"><label>Date<input type="date" name="date" value="${todayIso()}" required></label>
    <label>What kind<select name="type" id="act-type">${Object.entries(d.activity_types).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select></label>
    <label>Title<input name="title" placeholder="e.g. Q1 policy report, DC Fly-In"></label>
    <label id="act-pac-l">PAC<select name="pac"><option value="">–</option>${d.pacs.map((p) => `<option>${esc(p)}</option>`).join("")}</select></label>
    <label id="act-att-l">Board attendance %<input name="attendance" type="number" min="0" max="100"></label></div>
    <label class="stack">Notes<input name="notes"></label><div class="row-end"><button class="btn" type="submit">Save activity</button></div></form>`;
}
async function renderGoalLog() {
  $("#goals-log").innerHTML = `<div class="two-col">${entryForm("donor")}${entryForm("activity")}</div><div id="glog-list"><p class="hint">Loading…</p></div>`;
  const syncAct = () => { const t = $("#act-type").value; $("#act-pac-l").hidden = !["pac_board", "forum", "pac_review"].includes(t); $("#act-att-l").hidden = t !== "pac_board"; };
  $("#act-type").addEventListener("change", syncAct); syncAct();
  for (const [id, kind] of [["#donor-form", "donor"], ["#activity-form", "activity"]]) {
    $(id).addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { kind, ...Object.fromEntries(new FormData(e.target)) };
      try { await api("/api/goals/log", { method: "POST", body }); toast("Saved"); G.data = await api(`/api/goals?year=${G.year}`); renderGoalLog(); }
      catch (err) { toast(err.message, true); }
    });
  }
  let rows = [];
  try { rows = (await api(`/api/goals/log?year=${G.year}`)).filter((r) => r.kind !== "meeting"); } catch { /* shown empty */ }
  const types = G.data.activity_types;
  $("#glog-list").innerHTML = rows.length ? `<table class="data log-table"><thead><tr><th>Date</th><th>Entry</th><th>Details</th><th></th></tr></thead><tbody>${rows.map((r) => `
    <tr><td>${fmtDate(r.date)}</td><td>${r.kind === "donor" ? `<b>${esc(r.name)}</b> · ${esc(r.pac)} donor (${esc(r.status)})` : `<b>${esc(types[r.type] || r.type)}</b>${r.title ? ` · ${esc(r.title)}` : ""}`}</td>
    <td>${esc([r.amount, r.pac && r.kind === "activity" ? r.pac : "", r.attendance ? `${r.attendance}% attendance` : "", r.notes].filter(Boolean).join(" · "))}</td>
    <td><button class="btn btn-ghost btn-sm" type="button" data-del-entry="${r.id}">Delete</button></td></tr>`).join("")}</tbody></table>`
    : `<p class="hint">Nothing logged for ${G.year} yet.</p>`;
  document.querySelectorAll("[data-del-entry]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this entry?")) return;
    await api(`/api/goals/log/${b.dataset.delEntry}`, { method: "DELETE" }); G.data = await api(`/api/goals?year=${G.year}`); renderGoalLog();
  }));
}
function renderMilestones() {
  const ms = G.data.milestones;
  $("#goals-milestones").innerHTML = G.data.goals.map((g) => `<section class="panel wide"><h2>Goal ${g.n}: ${esc(g.title)}</h2>
    ${[1, 2, 3, 4].map((q) => { const items = ms.filter((m) => m.goal === g.n && m.quarter === q); return items.length ? `<h3 class="sub-h">Q${q}</h3>
      ${items.map((m) => `<label class="check ms${m.late ? " late" : ""}" for="ms-${m.id}"><input type="checkbox" id="ms-${m.id}" data-ms="${m.id}" ${m.done ? "checked" : ""}>
        ${esc(m.label)} <span class="hint">${m.done ? `done ${fmtDate(m.done.at.slice(0, 10))}` : `due ${fmtDate(m.due)}${m.late ? " · overdue" : ""}`}</span></label>`).join("")}` : ""; }).join("")}</section>`).join("");
  document.querySelectorAll("[data-ms]").forEach((c) => c.addEventListener("change", async () => {
    try { G.data = await api("/api/goals/milestone", { method: "POST", body: { year: G.year, id: c.dataset.ms, done: c.checked } }); renderMilestones(); }
    catch (e) { toast(e.message, true); }
  }));
}

// ------------------------------------------------------------------ meetings log
const MT = { year: new Date().getFullYear(), editing: null, rows: [] };
const LEVELS = { local: "Municipal / county", state: "State", federal: "Federal", other: "Other policy leader" };
function meetingForm(e = {}) {
  return `<h2>${e.id ? "Edit meeting" : "Log a meeting"}</h2>
    <label class="stack">Who met (one name per line)<textarea name="people" rows="3" required placeholder="Rep. Jane Doe&#10;Sen. John Smith">${esc(e.people || "")}</textarea></label>
    <div class="form-grid">
      <label>Date<input type="date" name="date" value="${esc(e.date || todayIso())}" required></label>
      <label>Level<select name="level">${Object.entries(LEVELS).map(([k, v]) => `<option value="${k}" ${k === (e.level || "state") ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      <label>Organization or office<input name="entity" value="${esc(e.entity || "")}" placeholder="e.g. Oklahoma House, City of Tulsa"></label>
      <label>Chamber staff there<input name="staff" value="${esc(e.staff || "")}" placeholder="Who from the Chamber"></label>
    </div>
    <label class="stack">Overview of the discussion<textarea name="overview" rows="5" placeholder="What was discussed, what they said, where they stand">${esc(e.overview || "")}</textarea></label>
    <div class="form-grid">
      <label>Issues<input name="issues" value="${esc(e.issues || "")}" placeholder="e.g. housing, workforce"></label>
      <label>Bills discussed<input name="bills" value="${esc((e.bills_display || []).join(", "))}" placeholder="HB 1234, SB 56"></label>
    </div>
    <label class="stack">Follow-up<input name="follow_up" value="${esc(e.follow_up || "")}" placeholder="Next steps and who owns them"></label>
    <label class="check"><input type="checkbox" name="in_person" ${e.in_person === false ? "" : "checked"}> In person (counts toward the face-to-face contact goal)</label>
    <div class="row-end"><button class="btn" type="submit">${e.id ? "Save changes" : "Save meeting"}</button><button class="btn btn-ghost" type="button" id="mt-cancel">Cancel</button></div>`;
}
function openMeetingForm(e) {
  MT.editing = e ? e.id : null;
  const f = $("#mt-form");
  f.innerHTML = meetingForm(e || {});
  f.hidden = false;
  $("#mt-cancel").addEventListener("click", () => { f.hidden = true; MT.editing = null; });
  f.scrollIntoView({ behavior: "smooth", block: "start" });
  f.querySelector("textarea[name=people]").focus({ preventScroll: true });
}
async function saveMeeting(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = { kind: "meeting", ...Object.fromEntries(fd), in_person: fd.get("in_person") === "on" };
  try {
    await api(MT.editing ? `/api/goals/log/${MT.editing}` : "/api/goals/log", { method: MT.editing ? "PUT" : "POST", body });
    toast(MT.editing ? "Meeting updated" : "Meeting saved");
    $("#mt-form").hidden = true; MT.editing = null;
    if (body.date.slice(0, 4) !== String(MT.year)) MT.year = Number(body.date.slice(0, 4));
    loadMeetings();
  } catch (err) { toast(err.message, true); }
}
async function loadMeetings() {
  yearOptions("#mt-year", MT.year);
  const q = $("#mt-q").value.trim();
  try { MT.rows = await api(`/api/goals/log?kind=meeting&year=${MT.year}${q ? `&q=${encodeURIComponent(q)}` : ""}`); } catch (e) { $("#mt-list").innerHTML = `<li class="hint">${esc(e.message)}</li>`; return; }
  const people = MT.rows.filter((r) => r.in_person !== false).reduce((n, r) => n + Math.max(1, (r.people || "").split(/[\n;]+/).filter((x) => x.trim()).length), 0);
  $("#mt-meta").textContent = `${MT.rows.length} meeting${MT.rows.length === 1 ? "" : "s"} in ${MT.year}${q ? " matching your search" : ""} · ${people} face-to-face contact${people === 1 ? "" : "s"}`;
  $("#mt-list").innerHTML = MT.rows.map((r) => `<li class="log-card">
      <div class="log-top"><span class="date">${fmtDate(r.date)}</span><span class="chip">${esc(LEVELS[r.level] || "")}</span>${r.in_person === false ? '<span class="chip">Not in person</span>' : ""}</div>
      <h3>${esc((r.people || "").split(/[\n;]+/).filter((x) => x.trim()).join(", ") || r.entity)}</h3>
      ${r.entity ? `<p class="hint">${esc(r.entity)}${r.staff ? ` · with ${esc(r.staff)}` : ""}</p>` : r.staff ? `<p class="hint">With ${esc(r.staff)}</p>` : ""}
      ${r.overview ? `<p class="log-overview">${esc(r.overview)}</p>` : ""}
      ${r.issues ? `<p><b>Issues:</b> ${esc(r.issues)}</p>` : ""}
      ${r.bills_display && r.bills_display.length ? `<p><b>Bills:</b> ${r.bills.map((b, i) => `<button class="linklike" type="button" data-open-bill="${esc(b)}">${esc(r.bills_display[i])}</button>`).join(", ")}</p>` : ""}
      ${r.follow_up ? `<p><b>Follow-up:</b> ${esc(r.follow_up)}</p>` : ""}
      <div class="row-end"><button class="btn btn-ghost btn-sm" type="button" data-edit-mt="${r.id}">Edit</button><button class="btn btn-ghost btn-sm" type="button" data-del-mt="${r.id}">Delete</button></div>
    </li>`).join("") || `<li class="empty"><h2>No meetings logged${q ? " match your search" : ` for ${MT.year}`}</h2><p>Tap “Log a meeting” after you meet with a legislator, mayor, commissioner or other policy leader.</p></li>`;
  document.querySelectorAll("#mt-list [data-open-bill]").forEach((b) => b.addEventListener("click", () => openBill(state.session, b.dataset.openBill)));
  document.querySelectorAll("[data-edit-mt]").forEach((b) => b.addEventListener("click", () => openMeetingForm(MT.rows.find((r) => r.id === Number(b.dataset.editMt)))));
  document.querySelectorAll("[data-del-mt]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this meeting?")) return;
    await api(`/api/goals/log/${b.dataset.delMt}`, { method: "DELETE" }); toast("Meeting deleted"); loadMeetings();
  }));
}

// ------------------------------------------------------------------ Friday report
const RP = { week: null };
async function loadReport() {
  if (!RP.week) RP.week = mondayOf(new Date());
  const wk = isoDate(RP.week);
  const scope = $("#rp-scope").value;
  $("#rp-week").textContent = isoDate(mondayOf(new Date())) === wk ? "This week" : `Week of ${RP.week.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;
  $("#rp-download").href = `/api/report.xlsx?session=${state.session}&week=${wk}&scope=${scope}`;
  $("#rp-table").innerHTML = `<p class="hint">Loading…</p>`;
  let d;
  try { d = await api(`/api/report?session=${state.session}&week=${wk}&scope=${scope}`); } catch (e) { $("#rp-table").innerHTML = `<p class="hint">${esc(e.message)}</p>`; return; }
  $("#rp-download").setAttribute("download", d.filename);
  $("#rp-count").textContent = `${d.rows.length} bill${d.rows.length === 1 ? "" : "s"} with action · ${d.filename}`;
  $("#rp-table").innerHTML = d.rows.length ? `<table class="data report"><thead><tr><th>Track</th><th>Bill</th><th>AI Summary</th><th>Last action date</th></tr></thead><tbody>
    ${d.rows.map((r) => `<tr><td>${esc(r.track)}</td><td><button class="linklike" type="button" data-open-bill="${esc(r.bill)}"><b>${esc(r.display)}</b></button></td>
      <td>${esc(r.summary)}<div class="hint">${esc(r.last_action)}</div></td><td>${fmtDate(r.date)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty"><h2>No OneVoice or tracked bills had action this week</h2><p>During session this fills in as bills move. Use the arrows to look at another week.</p></div>`;
  document.querySelectorAll("#rp-table [data-open-bill]").forEach((b) => b.addEventListener("click", () => openBill(state.session, b.dataset.openBill)));
}

// ------------------------------------------------------------------ Outlook link
async function renderIcal(fresh) {
  const s = fresh || (state.settings && state.settings.ical);
  if (!s) return;
  const url = $("#ical-all").checked ? s.all : s.hearings;
  $("#ical-url").value = url;
  $("#ical-open").href = url.replace(/^https?:\/\//, "webcal://");
}

// ------------------------------------------------------------------ bill versions in the drawer
function versionsBlock(d) {
  const v = d.versions || [];
  const reds = d.redlines || [];
  if (v.length < 2 && !reds.length) return "";
  return `<div class="d-versions"><h3 class="section-h">What changed?</h3>
    ${reds.map((r) => `<div class="redline-item"><button class="linklike" type="button" data-redline="${r.id}"><b>${esc(r.old)} → ${esc(r.new)}</b></button>
      <span class="hint">${fmtDate(r.at.slice(0, 10))} · ${esc(r.headline)}</span></div>`).join("")}
    ${v.length >= 2 ? `<form class="compare" id="cmp-form"><label>From<select id="cmp-a">${v.map((x, i) => `<option value="${esc(x.url)}" ${i === 0 ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>
      <label>To<select id="cmp-b">${v.map((x, i) => `<option value="${esc(x.url)}" ${i === v.length - 1 ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>
      <button class="btn btn-sm" type="submit">Compare</button></form>` : ""}
    <div id="cmp-out"></div></div>`;
}
function showRedline(r) {
  $("#cmp-out").innerHTML = `<p><b>${esc(r.old)} → ${esc(r.new)}</b>: ${esc(r.headline)}</p>
    ${r.gained && r.gained.length ? `<p class="kw-gain">Now mentions: ${esc(r.gained.join(", "))}</p>` : ""}
    ${r.lost && r.lost.length ? `<p class="kw-lost">No longer mentions: ${esc(r.lost.join(", "))}</p>` : ""}
    <p class="hint redline-key"><del>struck red</del> = taken out · <ins>green</ins> = added</p><div class="redline">${r.html}</div>`;
}
function bindVersions(d) {
  document.querySelectorAll("[data-redline]").forEach((b) => b.addEventListener("click", async () => {
    try { showRedline(await api(`/api/redline/${b.dataset.redline}`)); } catch (e) { toast(e.message, true); }
  }));
  const f = $("#cmp-form");
  if (!f) return;
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    if ($("#cmp-a").value === $("#cmp-b").value) { toast("Pick two different versions.", true); return; }
    $("#cmp-out").innerHTML = `<p class="hint">Reading both versions…</p>`;
    try { showRedline(await api(`/api/bill/${d.session}/${d.bill}/compare?a=${encodeURIComponent($("#cmp-a").value)}&b=${encodeURIComponent($("#cmp-b").value)}`)); }
    catch (err) { $("#cmp-out").innerHTML = `<p class="hint">${esc(err.message)}</p>`; }
  });
}

// ------------------------------------------------------------------ legislator directory
const DIR = { rows: null };
async function showDirectory() {
  $("#dir-profile").hidden = true; $("#dir-list-wrap").hidden = false;
  if (!DIR.rows) {
    $("#dir-list").innerHTML = `<li class="hint">Loading…</li>`;
    try { const d = await api(`/api/legislators?session=${state.session}`); DIR.rows = d.members; DIR.updated = d.updated; }
    catch (e) { $("#dir-list").innerHTML = `<li class="hint">${esc(e.message)}</li>`; return; }
  }
  renderDirectory();
}
function legTitle(r) { return `${r.chamber === "House" ? "Rep." : "Sen."} ${r.name}`; }
function partyLetter(p) { return /^rep/i.test(p) ? "R" : /^dem/i.test(p) ? "D" : (p || "").slice(0, 1); }
function renderDirectory() {
  const rows = DIR.rows || [];
  if (!rows.length) {
    $("#dir-list").innerHTML = `<li class="empty"><h2>The directory is loading for the first time</h2><p>It reads every member’s page from the House and Senate websites, which takes about two minutes. Tap “Refresh” above if it doesn’t appear.</p></li>`;
    return;
  }
  const q = $("#dir-q").value.trim().toLowerCase(), ch = $("#dir-chamber").value, area = $("#dir-area").value, party = $("#dir-party").value, sort = $("#dir-sort").value;
  let list = rows.filter((r) => (!ch || r.chamber === ch) && (!party || r.party === party) && (!area || r[area]) &&
    (!q || [r.name, r.hometown, r.counties.join(" "), r.cities.join(" "), r.committees.join(" "), `district ${r.district}`, r.leadership].join(" ").toLowerCase().includes(q)));
  const by = { name: (a, b) => a.last.localeCompare(b.last) || a.first.localeCompare(b.first), onevoice: (a, b) => b.onevoice_count - a.onevoice_count || a.last.localeCompare(b.last),
    meetings: (a, b) => b.meetings_count - a.meetings_count || a.last.localeCompare(b.last), district: (a, b) => a.chamber.localeCompare(b.chamber) || a.district - b.district };
  list = list.slice().sort(by[sort]);
  $("#dir-meta").textContent = `${list.length} of ${rows.length} members` + (DIR.updated ? ` · updated ${ago(DIR.updated)}` : "");
  $("#dir-list").innerHTML = list.map((r) => `<li><a class="leg-card" href="#legislator-${esc(r.key.replace(":", "-"))}">
      ${r.photo ? `<img src="${esc(r.photo)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-initials="${esc((r.first[0] || "") + (r.last[0] || ""))}">` : `<span class="leg-ph" aria-hidden="true">${esc((r.first[0] || "") + (r.last[0] || ""))}</span>`}
      <span class="leg-main"><b>${esc(legTitle(r))}</b>
        <span class="hint">${esc(partyLetter(r.party))}-${esc(r.chamber)} District ${esc(r.district)}${r.hometown ? ` · ${esc(r.hometown.replace(/, Oklahoma$/, ""))}` : ""}</span>
        ${r.leadership ? `<span class="hint">${esc(r.leadership)}</span>` : ""}
        <span class="leg-chips">${r.tulsa ? '<span class="chip chip-tulsa">Tulsa County</span>' : r.region ? '<span class="chip chip-tulsa">Tulsa region</span>' : ""}
          ${r.onevoice_count ? `<span class="chip">${r.onevoice_count} OneVoice bill${r.onevoice_count === 1 ? "" : "s"}</span>` : ""}
          ${r.meetings_count ? `<span class="chip">${r.meetings_count} meeting${r.meetings_count === 1 ? "" : "s"}</span>` : ""}</span></span></a></li>`).join("")
    || `<li class="empty"><h2>No one matches</h2><p>Try a different name, town or county.</p></li>`;
  photoFallback($("#dir-list"));
}
async function openLegislator(slugKey) {
  const [ch, ...rest] = slugKey.split("-");
  const slug = rest.join("-");
  $("#dir-list-wrap").hidden = true;
  const box = $("#dir-profile"); box.hidden = false;
  box.innerHTML = `<p class="hint">Loading…</p>`;
  let r;
  try { r = await api(`/api/legislators/${ch}/${encodeURIComponent(slug)}?session=${state.session}`); }
  catch (e) { box.innerHTML = `<a class="btn btn-ghost btn-sm" href="#directory">‹ All legislators</a><p class="hint">${esc(e.message)}</p>`; return; }
  const contact = [r.email ? `<a class="btn btn-sm" href="mailto:${esc(r.email)}">Email</a>` : r.contact_url ? `<a class="btn btn-sm" href="${esc(r.contact_url)}" target="_blank" rel="noopener">Email (Senate form)</a>` : "",
    r.phone ? `<a class="btn btn-ghost btn-sm" href="tel:${esc(r.phone.replace(/[^0-9+]/g, ""))}">Call ${esc(r.phone)}</a>` : "",
    `<a class="btn btn-ghost btn-sm" href="${esc(r.page_url)}" target="_blank" rel="noopener">Official page</a>`].join("");
  box.innerHTML = `<a class="btn btn-ghost btn-sm" href="#directory">‹ All legislators</a>
    <div class="leg-head">${r.photo ? `<img src="${esc(r.photo)}" alt="" referrerpolicy="no-referrer" data-initials="${esc((r.first[0] || "") + (r.last[0] || ""))}">` : ""}
      <div><h2>${esc(legTitle(r))}</h2>
        <p>${esc(r.party)} · ${esc(r.chamber)} District ${esc(r.district)}${r.hometown ? ` · ${esc(r.hometown)}` : ""}</p>
        ${r.leadership ? `<p class="hint">${esc(r.leadership)}</p>` : ""}
        <div class="row-end">${contact}</div></div></div>
    <div class="leg-grid">
      <section class="panel"><h3 class="section-h">Office</h3>
        <p>${esc(r.office || "–")}</p>${r.assistant ? `<p><b>Assistant:</b> ${esc(r.assistant)}</p>` : ""}
        ${r.email ? `<p><b>Email:</b> <a href="mailto:${esc(r.email)}">${esc(r.email)}</a></p>` : ""}
        ${r.term_limited ? `<p class="hint">Term limited in ${esc(r.term_limited)}</p>` : ""}</section>
      <section class="panel"><h3 class="section-h">District</h3>
        <p><b>Counties:</b> ${esc(r.counties.join(", ") || "–")}</p>
        ${r.cities.length ? `<p><b>Towns:</b> ${esc(r.cities.join(", "))}</p>` : ""}</section>
      <section class="panel"><h3 class="section-h">Committees</h3>
        ${r.committees.length ? `<ul class="plain">${r.committees.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : `<p class="hint">None listed.</p>`}</section>
    </div>
    ${r.hearings.length ? `<section class="panel wide"><h3 class="section-h">Their upcoming committee meetings</h3><ul class="plain">${r.hearings.map((m) => `<li><a href="#calendar">${esc(fmtWhen(m.start, m.time_text))} · ${esc(m.committee)}</a>${m.bills.length ? ` <span class="hint">· ${esc(m.bills.slice(0, 8).join(", "))}</span>` : ""}</li>`).join("")}</ul></section>` : ""}
    <section class="panel wide"><div class="row-between"><h3 class="section-h">Meetings with the Chamber (${r.meetings.length})</h3>
      <button class="btn btn-sm" type="button" id="leg-log">+ Log a meeting with ${esc(r.chamber === "House" ? "Rep." : "Sen.")} ${esc(r.last)}</button></div>
      ${r.meetings.length ? `<ol class="log-list">${r.meetings.map((e) => `<li class="log-card"><div class="log-top"><span class="date">${fmtDate(e.date)}</span>${e.staff ? `<span class="hint">with ${esc(e.staff)}</span>` : ""}</div>
        ${e.overview ? `<p class="log-overview">${esc(e.overview)}</p>` : ""}${e.follow_up ? `<p><b>Follow-up:</b> ${esc(e.follow_up)}</p>` : ""}</li>`).join("")}</ol>`
        : `<p class="hint">No meetings logged yet. Meetings count here when the legislator’s name is in “Who met.”</p>`}</section>
    <section class="panel wide"><h3 class="section-h">Bills they authored (${r.bills.length}${r.onevoice_count ? `, ${r.onevoice_count} OneVoice` : ""})</h3>
      ${r.bills.length ? `<ul class="leg-bills">${r.bills.map((b) => `<li><button class="linklike" type="button" data-open-bill="${esc(b.bill)}"><b>${esc(b.display)}</b></button>
        ${b.onevoice.map((t) => `<span class="chip chip-ov">${esc(t)}</span>`).join("")}${b.position ? `<span class="pill pos-${esc(b.position)}">${esc(cap(b.position))}</span>` : ""}
        <div>${esc(b.title)}</div><div class="hint">${b.role === "primary" ? "Author" : "Principal author in the other chamber"} · ${esc(b.stage_label)}</div></li>`).join("")}</ul>`
        : `<p class="hint">No bills found yet. This lists OneVoice and tracked bills, plus any bill you’ve opened, this session.</p>`}</section>`;
  photoFallback(box);
  box.querySelectorAll("[data-open-bill]").forEach((b) => b.addEventListener("click", () => openBill(state.session, b.dataset.openBill)));
  $("#leg-log").addEventListener("click", () => {
    location.hash = "#meetings";
    setTimeout(() => openMeetingForm({ people: legTitle(r), level: "state", entity: r.chamber === "House" ? "Oklahoma House of Representatives" : "Oklahoma State Senate" }), 150);
  });
  window.scrollTo(0, 0);
}
function photoFallback(root) {
  root.querySelectorAll("img").forEach((img) => img.addEventListener("error", () => {
    const span = document.createElement("span"); span.className = "leg-ph"; span.setAttribute("aria-hidden", "true");
    span.textContent = img.dataset.initials || ""; img.replaceWith(span);
  }, { once: true }));
}
function bindDirectory() {
  let t;
  $("#dir-q").addEventListener("input", () => { clearTimeout(t); t = setTimeout(renderDirectory, 150); });
  ["#dir-chamber", "#dir-area", "#dir-party", "#dir-sort"].forEach((id) => $(id).addEventListener("change", renderDirectory));
  $("#dir-refresh").addEventListener("click", async () => {
    try { await api("/api/legislators/refresh", { method: "POST" }); toast("Reading every member’s page. This takes about two minutes."); pollStatus(); }
    catch (e) { toast(e.message, true); }
  });
}

function bindNewFeatures() {
  bindDirectory();
  $("#sb-table-toggle").addEventListener("click", (e) => { sbTables = !sbTables; e.target.setAttribute("aria-pressed", String(sbTables)); e.target.textContent = sbTables ? "Show as charts" : "Show as tables"; loadScoreboard(); });
  $("#goals-year").addEventListener("change", (e) => { G.year = Number(e.target.value); loadGoals(); });
  document.querySelectorAll("#goals-tabs [data-gtab]").forEach((b) => b.addEventListener("click", () => { G.tab = b.dataset.gtab; renderGoalTabs(); }));
  $("#mt-add").addEventListener("click", () => openMeetingForm(null));
  $("#mt-form").addEventListener("submit", saveMeeting);
  let mtTimer;
  $("#mt-q").addEventListener("input", () => { clearTimeout(mtTimer); mtTimer = setTimeout(loadMeetings, 250); });
  $("#mt-year").addEventListener("change", (e) => { MT.year = Number(e.target.value); loadMeetings(); });
  $("#rp-prev").addEventListener("click", () => { RP.week.setDate(RP.week.getDate() - 7); loadReport(); });
  $("#rp-next").addEventListener("click", () => { RP.week.setDate(RP.week.getDate() + 7); loadReport(); });
  $("#rp-scope").addEventListener("change", loadReport);
  $("#rp-email").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { const r = await api("/api/report/send", { method: "POST", body: { week: isoDate(RP.week) } }); toast(`Sent to ${r.to.join(", ")}`); }
    catch (err) { toast(err.message, true); } finally { e.target.disabled = false; }
  });
  $("#rp-prepare").addEventListener("click", async () => {
    try { await api("/api/report/prepare", { method: "POST", body: { session: state.session, week: isoDate(RP.week) } });
      toast("Re-reading this week’s bills from oklegislature.gov. The list refreshes when it’s done."); pollStatus(); }
    catch (err) { toast(err.message, true); }
  });
  $("#ical-all").addEventListener("change", () => renderIcal());
  $("#ical-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#ical-url").value); toast("Calendar link copied"); }
    catch { $("#ical-url").select(); toast("Press Ctrl+C (or long-press) to copy"); }
  });
  $("#ical-reset").addEventListener("click", () => {
    $("#ical-reset-area").innerHTML = `<span class="confirm">The old link will stop working. <button class="btn btn-danger btn-sm" type="button" id="ical-reset-yes">Make a new link</button></span>`;
    $("#ical-reset-yes").addEventListener("click", async () => {
      const r = await api("/api/ical/reset", { method: "POST" });
      state.settings.ical = r; renderIcal(r); toast("New calendar link made");
      $("#ical-reset-area").innerHTML = `<button class="btn btn-ghost btn-sm" type="button" id="ical-reset">Make a new link</button>`;
    });
  });
  $("#cal-outlook").addEventListener("click", (e) => { e.preventDefault(); location.hash = "#settings"; setTimeout(() => $("#settings-outlook").scrollIntoView({ behavior: "smooth" }), 50); });
}

boot().catch((e) => {
  document.querySelector("main").innerHTML = `<div class="empty"><h2>The tracker could not start</h2><p>${esc(e.message)}</p></div>`;
});
