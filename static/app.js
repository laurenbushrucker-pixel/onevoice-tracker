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
  drawer: null, // { session, bill, data }
  editing: null, // priority id whose keywords are open
};
const VIEWS = ["agenda", "mine", "find", "updates", "settings"];
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
    return p ? `<span class="agenda-tag" title="${esc(p.task_force)} #${p.number}: ${esc(p.title)} (matched: ${esc(a.keywords.join(", "))})">${esc(shortLabel(p))}</span>` : "";
  }).join("")}</div>`;
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

  bindEvents();
  await loadSession();
  route();
  setInterval(pollStatus, 4000);
}

function fillAgendaSelects() {
  const opts = state.config.agenda.map((p) => `<option value="${p.id}">${esc(shortLabel(p))}: ${esc(p.title)}</option>`).join("");
  $("#find-agenda").innerHTML = `<option value="">All bills</option><option value="any">Matches any agenda item</option>${opts}`;
  $("#mine-agenda").innerHTML = `<option value="">Any agenda item</option><option value="any">Matches the agenda</option>${opts}`;
}

function bindEvents() {
  window.addEventListener("hashchange", route);
  $("#session").addEventListener("change", async (e) => {
    state.session = e.target.value;
    try { localStorage.setItem("ovt.session", state.session); } catch { /* ignore */ }
    state.find = { offset: 0, total: 0, results: [] };
    await loadSession();
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
  ["#find-agenda", "#find-subject", "#find-chamber", "#find-stage", "#find-sort"].forEach((s) => $(s).addEventListener("change", () => {
    if (location.hash.startsWith("#bills-")) history.replaceState(null, "", "#find");
    $("#find-back").hidden = true;
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
  $("#s-test").addEventListener("click", testEmail);
  $("#agenda-reset").addEventListener("click", confirmReset);
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
  return VIEWS.includes(h) ? h : "agenda";
}
function route() {
  const view = currentView();
  const h = location.hash.replace("#", "");
  if (h.startsWith("bills-") && priorityById(h.slice(6))) {
    // A priority's bill list: show every bill matching that agenda item, newest first.
    $("#find-agenda").value = h.slice(6);
    $("#find-q").value = ""; $("#find-subject").value = ""; $("#find-stage").value = ""; $("#find-chamber").value = "";
    $("#find-sort").value = "newest";
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
}

// ------------------------------------------------------------------ status
let lastJobFinished = null;
async function pollStatus() {
  try {
    state.status = await api(`/api/status?session=${state.session}`);
  } catch { return; }
  const s = state.status, job = s.job || {};
  const bar = $("#statusbar");
  if (job.running) {
    const pct = job.total ? Math.round((100 * job.done) / job.total) : 0;
    const what = { index: "Building search index", filings: "Looking for newly filed bills", refresh: "Checking tracked bills", summaries: "Getting bill summaries", texts: "Reading bill texts" }[job.kind] || "Working";
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
      let msg = { index: "Search index is ready", refresh: "Tracked bills are up to date", filings: "Finished checking for new filings",
        texts: "Bill texts updated",
        summaries: job.result ? `Updated ${job.result.summarized} summar${job.result.summarized === 1 ? "y" : "ies"}` : "Summaries updated" }[job.kind];
      if (job.kind === "filings" && job.result) {
        const r = job.result;
        msg = `${r.new_bills} new bill${r.new_bills === 1 ? "" : "s"} found, ${r.new_matches} agenda match${r.new_matches === 1 ? "" : "es"}` +
          (r.sent ? `, alert emailed for ${r.sent}` : "") + (r.error ? `. Email failed: ${r.error}` : "");
      }
      toast(job.error ? `Update failed: ${job.error}` : msg, !!(job.error || (job.result && job.result.error)));
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
    if (state.editing) previewKeywords(state.editing);
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
        <div class="row-end"><button class="btn btn-sm" type="submit">Save keywords</button></div>
      </form>` : ""}
    </article>`;
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

  const watching = state.status ? state.status.alerts_watch : s.alert_sessions.some((x) => x.code === state.session);
  const on = s.alerts_enabled && s.email_configured && s.recipients.length;
  $("#alert-state").className = "alert-state" + (on ? "" : " off");
  $("#alert-state").innerHTML = !watching
    ? `Email alerts cover the ${esc(sessions)} only.`
    : on ? `Email alerts <b>on</b> for ${s.recipients.length} recipient${s.recipients.length === 1 ? "" : "s"}.`
      : `Email alerts <b>off</b>. <a href="#settings">Set up alerts</a>`;
}

async function saveSettings(e) {
  e.preventDefault();
  try {
    await api("/api/settings", { method: "PUT", body: {
      alerts_enabled: $("#s-enabled").checked,
      recipients: $("#s-recipients").value.split(/[\n,;]+/).map((x) => x.trim()).filter(Boolean),
      app_url: $("#s-url").value.trim(),
    } });
    toast("Settings saved");
    $("#s-recipients").blur(); $("#s-url").blur();
    loadSettings();
  } catch (err) { toast(err.message, true); }
}

async function testEmail() {
  $("#s-test").disabled = true;
  try {
    const r = await api("/api/settings/test-email", { method: "POST" });
    toast(`Test email sent to ${r.to.join(", ")}`);
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
      <td class="cell-pos"><select data-field="position" class="pos-${esc(t.position)}" aria-label="Position on ${esc(t.display)}">${optionList(state.config.positions, t.position)}</select></td>
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
        <p class="last"><b>Last action:</b> ${esc(r.last_action || "No action recorded")}${r.last_action_date ? " · " + fmtDate(r.last_action_date) : ""}</p>
        ${officialTitle(r)}
        ${agendaTags(r.agenda)}
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
  const authors = [d.author, d.other_author].filter(Boolean).join(" and ");
  const agendaBlock = (d.agenda || []).map((a) => {
    const p = priorityById(a.id);
    return p ? `<div><b>${esc(p.task_force)} #${p.number}:</b> ${esc(p.title)} <span class="kw">(matched: ${esc(a.keywords.join(", "))})</span></div>` : "";
  }).join("");

  $("#drawer-body").innerHTML = `
    <div class="d-title">
      <span class="billno">${esc(d.display)}</span> ${stagePill(d.stage, d.stage_label, closed)}
      <h2>${esc(d.title || "Title not yet available")}</h2>
      <div class="d-meta">
        ${authors ? `<span>By ${esc(authors)}</span>` : ""}
        ${d.coauthors.length ? `<span>Coauthors: ${esc(d.coauthors.join(", "))}</span>` : ""}
        ${d.subjects.length ? `<span>Subjects: ${esc(d.subjects.join(", "))}</span>` : ""}
        <span>Fetched ${ago(d.fetched_at)}${d.stale ? " (site unreachable, showing saved copy)" : ""}</span>
      </div>
    </div>

    <div class="d-ai" id="d-ai">${aiBlock(d)}</div>

    ${agendaBlock ? `<div class="d-agenda"><h3 class="section-h" style="margin:0">OneVoice agenda</h3>${agendaBlock}</div>` : ""}

    <div>
      <div class="progress ${esc(d.stage)}">${STAGE_ORDER.map((_, i) => `<div class="${i <= reach ? "on" : ""}"></div>`).join("")}</div>
      <div class="progress-labels">${PROGRESS_LABELS.map((l) => `<span>${l}</span>`).join("")}</div>
    </div>

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
      </div>
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
  if (t) {
    $("#t-position").addEventListener("change", (e) => saveTracking(d.bill, { position: e.target.value }));
    $("#t-priority").addEventListener("change", (e) => saveTracking(d.bill, { priority: e.target.value }));
    $("#track-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const row = await saveTracking(d.bill, { notes: $("#t-notes").value, labels: $("#t-labels").value });
      if (row) d.tracked = { ...d.tracked, notes: row.notes, labels: row.labels };
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

boot().catch((e) => {
  document.querySelector("main").innerHTML = `<div class="empty"><h2>The tracker could not start</h2><p>${esc(e.message)}</p></div>`;
});
