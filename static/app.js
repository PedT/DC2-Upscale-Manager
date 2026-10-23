// DC2 Upscale Manager - browser side (M3: setup, library, runs). Plain JS, no build step.
"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  items: [], byId: new Map(), shown: [], selected: new Set(), lastClick: null,
  version: null, lastEvent: 0, config: null, models: [], jobs: null, tab: "library", cfgDirty: false,
};
const STATUS_LABEL = { todo: "to do", queued: "queued", upscaling: "upscaling", review: "in review", approved: "approved" };
const KEY_TIP = {
  verified: "Replacement key confirmed in game",
  computed: "Replacement key computed exactly (menus)",
  predicted: "Replacement key predicted; install blocked until confirmed",
  unknown: "No replacement key",
};

// ---------- helpers ----------
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function fmtS(s) {
  if (s == null) return "--:--";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? `${h}:${String(m).padStart(2, "0")}` : String(m).padStart(2, "0")) + ":" + String(x).padStart(2, "0");
}
function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? 12000 : 7000);
}

// ---------- tabs ----------
function showTab(name) {
  if (!document.getElementById(`tab-${name}`)) name = "library";
  S.tab = name;
  history.replaceState(null, "", `#${name}`);
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${name}`));
  if (name === "runs") loadRuns();
  if (name === "review" && typeof Review !== "undefined") Review.load();
  if (name === "install" && typeof Install !== "undefined") Install.load();
  if (name === "setup" && typeof Maint !== "undefined") Maint.load();
}
$("tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && !b.disabled) showTab(b.dataset.tab); });
document.addEventListener("click", (e) => { const a = e.target.closest("[data-goto]"); if (a) { e.preventDefault(); showTab(a.dataset.goto); } });

// ---------- polling ----------
async function poll() {
  try {
    const st = await api(`/api/state?since=${S.lastEvent}`);
    S.config = st.config;
    S.jobs = st.jobs;
    if (!S.cfgDirty) fillConfig(st.config);
    if (JSON.stringify(st.models) !== JSON.stringify(S.models)) { S.models = st.models; fillModels(); }
    renderSummary(st);
    renderJobbar(st.jobs);
    for (const ev of st.jobs.events) {
      S.lastEvent = Math.max(S.lastEvent, ev.id);
      toast(ev.text, ev.kind === "error" || ev.state === "failed" ? "bad" : "ok");
      if (S.tab === "runs") loadRuns();
      if (ev.kind === "run_done" && typeof Review !== "undefined") Review.runDone(ev);
      if (S.tab === "install" && typeof Install !== "undefined") Install.load();
    }
    renderExtractInfo(st);
    if (st.version !== S.version) {
      S.version = st.version;
      await loadItems();
      if (S.tab === "review" && typeof Review !== "undefined") Review.load();
    }
  } catch (e) {
    console.error(e);
  }
  const active = S.jobs && (S.jobs.current || S.jobs.queue.length);
  setTimeout(poll, active ? 700 : 2500);
}

function renderSummary(st) {
  const c = st.counts, sum = (k) => Object.values(c[k] || {}).reduce((a, b) => a + b, 0);
  const all = (s) => (c.fullscreen?.[s] || 0) + (c.room?.[s] || 0);
  $("summary").textContent = sum("fullscreen") + sum("room")
    ? `${sum("fullscreen")} menus · ${sum("room")} rooms · ${all("approved")} approved · ${all("review")} in review`
    : "no images extracted yet";
  $("reviewCount").textContent = all("review");
}

function renderJobbar(jobs) {
  const cur = jobs.current;
  $("jobbar").classList.toggle("hidden", !cur && !jobs.queue.length);
  $("jobQueue").textContent = jobs.queue.length ? `+${jobs.queue.length} queued` : "";
  if (!cur) { $("jobTitle").textContent = "Waiting…"; $("jobDetail").textContent = ""; $("jobFill").style.width = "0"; return; }
  $("jobTitle").textContent = cur.title;
  $("jobCancel").dataset.job = cur.id;
  const p = cur.progress;
  let pct = 0, detail = "starting…";
  if (p && cur.kind === "run") {
    pct = p.total ? (100 * (p.done + p.failed + p.current_pct / 100)) / p.total : 0;
    detail = `${p.done}/${p.total} done · ${p.failed} failed · current ${p.current_pct.toFixed(0)}% · ` +
             `elapsed ${fmtS(p.elapsed_s)} · ETA ${fmtS(p.eta_s)}`;
  } else if (p && (cur.kind === "extract" || cur.kind === "install")) {
    pct = p.total ? (100 * p.done) / p.total : 0;
    detail = p.total ? `${p.done}/${p.total} · ${p.message}` : p.message;
  }
  $("jobFill").style.width = `${Math.min(100, pct).toFixed(1)}%`;
  $("jobDetail").textContent = detail;
}
$("jobCancel").addEventListener("click", async (e) => {
  try { await api(`/api/jobs/${e.target.dataset.job}/cancel`, {}); toast("Cancelling…"); } catch (err) { toast(err.message, "bad"); }
});

// ---------- library ----------
async function loadItems() {
  S.items = await api("/api/items");
  S.byId = new Map(S.items.map((it) => [it.id, it]));
  for (const id of [...S.selected]) if (!S.byId.has(id)) S.selected.delete(id);
  fillStageAndTried();
  render();
  if (!S.items.length && S.tab === "library") showTab("setup");
}

function fillStageAndTried() {
  const stages = [...new Set(S.items.map((i) => i.stage).filter(Boolean))].sort();
  const tried = [...new Set(S.items.flatMap((i) => i.models_tried))].sort();
  syncOptions($("fStage"), stages, "All stages");
  syncOptions($("fTried"), tried.map((m) => [m, `Tried: ${m}`]), "Models tried: any", [["__none", "Not tried yet"]]);
}
function syncOptions(sel, values, firstLabel, extra = []) {
  const keep = sel.value;
  const opts = [["", firstLabel], ...extra, ...values.map((v) => (Array.isArray(v) ? v : [v, v]))];
  sel.innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  sel.value = opts.some(([v]) => v === keep) ? keep : "";
}
function fillModels() {
  const sel = $("rModel"), keep = sel.value || localStorage.getItem("model") || "ultrasharp-4x";
  sel.innerHTML = S.models.map((m) => `<option>${esc(m)}</option>`).join("");
  if (S.models.includes(keep)) sel.value = keep;
  $("modelsList").textContent = S.models.length ? `Models: ${S.models.join(", ")}` : "No models found.";
}

function filtered() {
  const kind = document.querySelector("#fKind .on").dataset.v, stage = $("fStage").value, status = $("fStatus").value;
  const tried = $("fTried").value, q = $("fSearch").value.trim().toLowerCase();
  const dups = $("fDups").checked, place = $("fPlace").checked;
  return S.items.filter((it) =>
    (!kind || it.kind === kind) && (!stage || it.stage === stage) && (!status || it.status === status) &&
    (!tried || (tried === "__none" ? !it.models_tried.length : it.models_tried.includes(tried))) &&
    (dups || !it.dup_of) && (place || !it.placeholder) &&
    (!q || `${it.id} ${it.label}`.toLowerCase().includes(q)));
}

function render() {
  S.shown = filtered();
  $("shownCount").textContent = `${S.shown.length} shown`;
  $("emptyHint").classList.toggle("hidden", S.items.length > 0);
  const html = S.shown.map((it, i) => {
    const tags = it.models_tried.map((m) => `<span class="tag" title="already tried">${esc(m)}</span>`).join("");
    const extra = it.dup_of ? `<span class="tag" title="identical to ${esc(it.dup_of)}">dup</span>` :
                  it.placeholder ? `<span class="tag">blank</span>` : "";
    const keyMark = `<span class="badge key-${it.key_status}" title="${esc(KEY_TIP[it.key_status] || "")}">${
      { verified: "✓ key", computed: "key", predicted: "~ key", unknown: "no key" }[it.key_status]}</span>`;
    const hint = it.status === "approved" || it.status === "review" ? "\nDouble-click to compare" : "";
    return `<div class="card-item${S.selected.has(it.id) ? " sel" : ""}" data-i="${i}" title="${esc(it.id)}${it.notes ? "\n" + esc(it.notes) : ""}${hint}">
      <span class="check"></span>
      <img loading="lazy" src="/ws/${esc(it.thumb)}" alt="">
      <div class="lbl">${esc(it.label)}</div>
      <div class="meta"><span class="badge st-${it.status}">${STATUS_LABEL[it.status]}</span>${
        it.installed ? `<span class="badge st-approved" title="installed in the game">installed</span>` : ""}${keyMark}${extra}${tags}</div>
    </div>`;
  }).join("");
  $("grid").innerHTML = html;
  updateSelection();
}

function updateSelection() {
  $("selCount").textContent = S.selected.size;
  const runnable = [...S.selected].filter((id) => { const it = S.byId.get(id); return it && it.status === "todo" && !it.dup_of && !it.placeholder; }).length;
  $("runBtn").disabled = !runnable || !S.models.length;
  $("runBtn").textContent = runnable ? `Upscale ${runnable} image${runnable > 1 ? "s" : ""}` : "Upscale";
  const approved = selectedApproved().length;
  $("reopenBtn").classList.toggle("hidden", !approved);
  $("reopenBtn").textContent = `Reopen ${approved} approved`;
}

function selectedApproved() {
  return [...S.selected].filter((id) => S.byId.get(id)?.status === "approved");
}

$("reopenBtn").addEventListener("click", async () => {
  const ids = selectedApproved();
  if (!ids.length) return;
  const installed = ids.filter((id) => S.byId.get(id).installed).length;
  if (!confirm(`Send ${ids.length} approved image(s) back to "to do"? Their approved versions will no longer be used.` +
      (installed ? `\n\n${installed} of them are installed: click "Install / update game" afterwards to remove or replace those files.` : ""))) return;
  try {
    const r = await api("/api/review/reopen", { item_ids: ids });
    const errs = Object.values(r.errors);
    toast(`${r.ok.length} image(s) reopened and still selected: pick a model and click Upscale to rerun them` +
          (errs.length ? `; ${errs.length} failed: ${errs[0]}` : ""), errs.length ? "bad" : "ok");
    S.version = null;                                    // refresh the grid on the next poll
  } catch (e) { toast(e.message, "bad"); }
});

$("grid").addEventListener("click", (e) => {
  const card = e.target.closest(".card-item");
  if (!card) return;
  const i = +card.dataset.i, it = S.shown[i];
  if (e.shiftKey && S.lastClick != null) {
    const [a, b] = [Math.min(S.lastClick, i), Math.max(S.lastClick, i)];
    for (let k = a; k <= b; k++) S.selected.add(S.shown[k].id);
    document.querySelectorAll(".card-item").forEach((c) => { const k = +c.dataset.i; if (k >= a && k <= b) c.classList.add("sel"); });
  } else {
    S.selected.has(it.id) ? S.selected.delete(it.id) : S.selected.add(it.id);
    card.classList.toggle("sel");
  }
  S.lastClick = i;
  updateSelection();
});
$("selAll").onclick = () => { S.shown.forEach((it) => S.selected.add(it.id)); render(); };
$("selTodo").onclick = () => { S.shown.filter((it) => it.status === "todo").forEach((it) => S.selected.add(it.id)); render(); };
$("selClear").onclick = () => { S.selected.clear(); S.lastClick = null; render(); };
$("fKind").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#fKind button").forEach((x) => x.classList.toggle("on", x === b));
  S.lastClick = null; render();
});
for (const id of ["fStage", "fStatus", "fTried", "fDups", "fPlace"]) $(id).addEventListener("change", () => { S.lastClick = null; render(); });
$("fSearch").addEventListener("input", () => { S.lastClick = null; render(); });
$("rModel").addEventListener("change", () => localStorage.setItem("model", $("rModel").value));

$("runBtn").addEventListener("click", async () => {
  const ids = [...S.selected];
  if (typeof Review !== "undefined") Review.askPermission();   // desktop notification when it finishes
  try {
    const r = await api("/api/run", { item_ids: ids, model: $("rModel").value, scale: +$("rScale").value,
                                     workers: +$("rWorkers").value, gpu: $("rGpu").value.trim() });
    toast(`Run ${r.run_id} queued: ${ids.length - r.skipped.length} image(s) with ${$("rModel").value}` +
          (r.skipped.length ? ` (${r.skipped.length} skipped: not "to do", duplicate or blank)` : ""));
    S.selected.clear();
    S.version = null;                     // force a refresh on the next poll
  } catch (e) { toast(e.message, "bad"); }
});

// ---------- runs ----------
async function loadRuns() {
  const runs = await api("/api/runs");
  const queuedJob = new Map((S.jobs?.queue || []).filter((j) => j.kind === "run").map((j) => [j.run_id, j.id]));
  $("runsBody").innerHTML = runs.map((r) => {
    const took = r.started && r.finished ? fmtS(r.finished - r.started) : r.state === "running" ? "running" : "";
    const started = r.started ? new Date(r.started * 1000).toLocaleString() : "";
    let action = "";
    if (r.state === "queued" && queuedJob.has(r.id)) action = `<button class="ghost danger" data-cancel="${queuedJob.get(r.id)}">Cancel</button>`;
    else if (r.leftovers) action = `<button class="ghost" data-resume="${r.id}">Resume ${r.leftovers} left over</button>`;
    const log = r.log_tail ? `<details><summary class="muted">log</summary><pre>${esc(r.log_tail)}</pre></details>` : "";
    return `<tr><td>${r.id}</td><td><span class="badge">${esc(r.state)}</span></td><td>${esc(r.model)}</td><td>×${r.scale}</td>
      <td>${r.done}/${r.total}</td><td>${r.failed || ""}</td><td>${started}</td><td>${took}</td><td>${action}${log}</td></tr>`;
  }).join("") || `<tr><td colspan="9" class="muted">No runs yet.</td></tr>`;
}
$("runsBody").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  try {
    if (b.dataset.cancel) { await api(`/api/jobs/${b.dataset.cancel}/cancel`, {}); toast("Run cancelled"); }
    if (b.dataset.resume) { const r = await api(`/api/runs/${b.dataset.resume}/resume`, {}); toast(`Run ${r.run_id} queued`); }
    S.version = null; setTimeout(loadRuns, 300);
  } catch (err) { toast(err.message, "bad"); }
});

// ---------- setup ----------
const CFG_FIELDS = { cGame: "game_dir", cWs: "workspace", cMod: "mod_dir", cBin: "upscayl_bin" };
function fillConfig(cfg) { for (const [id, key] of Object.entries(CFG_FIELDS)) $(id).value = cfg[key] || ""; }
for (const id of Object.keys(CFG_FIELDS)) $(id).addEventListener("input", () => { S.cfgDirty = true; });
document.querySelectorAll("[data-browse]").forEach((b) => b.addEventListener("click", async () => {
  const target = $(b.dataset.browse);
  b.disabled = true;
  try {
    const r = await api("/api/browse", { kind: b.dataset.kind, initial: target.value, title: target.previousElementSibling.textContent });
    if (r.path) { target.value = r.path; S.cfgDirty = true; }
  } catch (e) { toast(e.message, "bad"); }
  b.disabled = false;
}));
$("saveCfg").addEventListener("click", async () => {
  const body = {};
  for (const [id, key] of Object.entries(CFG_FIELDS)) body[key] = $(id).value;
  const msg = $("cfgMsg");
  try {
    const r = await api("/api/config", body);
    S.cfgDirty = false;
    msg.className = `msg ${r.warnings.length ? "warn" : "ok"}`;
    msg.textContent = r.warnings.length ? `Saved with warnings: ${r.warnings.join("; ")}` : "Saved.";
    S.version = null;
  } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
});
$("checkBtn").addEventListener("click", async () => {
  const msg = $("checkMsg");
  msg.className = "msg"; msg.textContent = "checking…";
  try {
    const r = await api("/api/selfcheck", {});
    msg.className = `msg ${r.problems.length ? "bad" : "ok"}`;
    msg.textContent = r.problems.length ? r.problems.join("; ") : "OK: upscayl-bin works and supports every option we use.";
  } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
});
$("extractBtn").addEventListener("click", async () => {
  try { await api("/api/extract", {}); toast("Extraction queued"); } catch (e) { toast(e.message, "bad"); }
});
function renderExtractInfo(st) {
  const s = st.last_extract;
  $("extractInfo").textContent = s
    ? `Last extraction: ${s.fullscreen} menu images, ${s.rooms} room views, ${s.duplicates} duplicates, ${s.placeholders} blank placeholders.`
    : "Not extracted yet.";
  $("exeWarnings").innerHTML = (st.exe_warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  if (st.recovered_runs.length && !S.recoveredShown) {
    S.recoveredShown = true;
    toast(`Run(s) ${st.recovered_runs.join(", ")} were interrupted when the app last closed. Finished images were kept; see the Runs tab to resume.`, "bad");
  }
}

if (location.hash.length > 1) showTab(location.hash.slice(1));
poll();
