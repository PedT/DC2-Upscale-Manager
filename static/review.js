// Review tab (M4): one-by-one comparison + bulk grid, approve/reject, run-finished notifications,
// and the detail popup (double-click a library card) with "replace with edited file" / "reopen".
// Uses helpers from app.js: $, S, api, esc, toast, showTab.
"use strict";

const Review = {
  queue: [], idx: 0, mode: "single", viewer: null, mdViewer: null, selected: new Set(), detail: null,

  init() {
    this.viewer = new Compare($("rvViewer"));
    for (const [id, key] of [["rvPixel", "pixelated"], ["rvDelete", "deleteRejected"], ["optNotify", "notify"], ["optChime", "chime"]]) {
      const saved = localStorage.getItem(key);
      if (saved !== null) $(id).checked = saved === "1";
      $(id).addEventListener("change", () => localStorage.setItem(key, $(id).checked ? "1" : "0"));
    }
    this.viewer.setPixelated($("rvPixel").checked);
    $("rvPixel").addEventListener("change", () => { this.viewer.setPixelated($("rvPixel").checked); this.mdViewer?.setPixelated($("rvPixel").checked); });
    $("optNotify").addEventListener("change", () => this.askPermission());
    $("rvMode").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) this.setMode(b.dataset.v); });
    $("rvFit").onclick = () => this.viewer.fit();
    $("rv11").onclick = () => this.viewer.oneToOne();
    $("rvZoomIn").onclick = () => this.viewer.zoomBy(1.25);
    $("rvZoomOut").onclick = () => this.viewer.zoomBy(0.8);
    $("rvApprove").onclick = () => this.decide("approve");
    $("rvReject").onclick = () => this.decide("reject");
    $("rvPrev").onclick = () => this.go(-1);
    $("rvNext").onclick = () => this.go(1);
    $("rvStrip").addEventListener("click", (e) => { const i = e.target.dataset.i; if (i != null) { this.idx = +i; this.renderSingle(); } });
    $("rvCards").addEventListener("click", (e) => this.gridClick(e));
    $("rvCards").addEventListener("dblclick", (e) => {
      const c = e.target.closest(".card-item"); if (!c) return;
      this.idx = +c.dataset.i; this.setMode("single");
    });
    $("rvCards").addEventListener("mousemove", (e) => {
      const hov = e.target.closest(".hov"); if (!hov) return;
      const r = hov.getBoundingClientRect(), x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
      hov.querySelector(".u").style.clipPath = `inset(0 0 0 ${x}px)`;
      hov.querySelector(".line").style.left = `${x}px`;
    });
    $("rvCards").addEventListener("mouseout", (e) => {
      const hov = e.target.closest(".hov"); if (hov && !hov.contains(e.relatedTarget)) hov.querySelector(".u").style.clipPath = "none";
    });
    $("rvSelAll").onclick = () => { this.queue.forEach((q) => this.selected.add(q.id)); this.renderGrid(); };
    $("rvSelClear").onclick = () => { this.selected.clear(); this.renderGrid(); };
    $("rvBulkApprove").onclick = () => this.bulk("approve");
    $("rvBulkReject").onclick = () => this.bulk("reject");
    $("bannerReview").onclick = () => { $("banner").classList.add("hidden"); showTab("review"); };
    $("bannerClose").onclick = () => $("banner").classList.add("hidden");
    document.addEventListener("keydown", (e) => this.key(e));
    // detail popup
    $("mdClose").onclick = () => this.closeDetail();
    $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") this.closeDetail(); });
    $("mdReplace").onclick = () => $("mdFile").click();
    $("mdFile").addEventListener("change", () => this.replaceFile());
    $("mdReopen").onclick = () => this.reopen();
    $("mdApprove").onclick = () => this.detailDecide("approve");
    $("mdReject").onclick = () => this.detailDecide("reject");
    $("grid").addEventListener("dblclick", (e) => {
      const c = e.target.closest(".card-item"); if (c) this.openDetail(S.shown[+c.dataset.i].id);
    });
  },

  // ---------- data ----------
  async load() {
    const keep = this.queue[this.idx]?.id;
    this.queue = await api("/api/review");
    const k = this.queue.findIndex((q) => q.id === keep);
    this.idx = k >= 0 ? k : Math.min(this.idx, Math.max(0, this.queue.length - 1));
    for (const id of [...this.selected]) if (!this.queue.some((q) => q.id === id)) this.selected.delete(id);
    this.render();
  },

  setMode(m) {
    this.mode = m;
    document.querySelectorAll("#rvMode button").forEach((b) => b.classList.toggle("on", b.dataset.v === m));
    this.render();
  },

  render() {
    const empty = !this.queue.length;
    $("rvEmpty").classList.toggle("hidden", !empty);
    $("rvSingle").classList.toggle("hidden", empty || this.mode !== "single");
    $("rvStrip").classList.toggle("hidden", empty || this.mode !== "single");
    $("rvGrid").classList.toggle("hidden", empty || this.mode !== "grid");
    document.querySelectorAll(".single-only").forEach((el) => el.classList.toggle("hidden", this.mode !== "single"));
    $("rvPos").textContent = empty ? "" : this.mode === "single" ? `${this.idx + 1} of ${this.queue.length} waiting` : `${this.queue.length} waiting`;
    if (empty) return;
    this.mode === "single" ? this.renderSingle() : this.renderGrid();
  },

  // ---------- one by one ----------
  renderSingle() {
    const q = this.queue[this.idx];
    if (!q) return;
    $("rvPos").textContent = `${this.idx + 1} of ${this.queue.length} waiting`;
    this.viewer.load(`/ws/${q.original_png}`, `/ws/${q.candidate.path}`, "Original 320×240", `Upscaled · ${q.candidate.model}`);
    $("rvLabel").textContent = q.label;
    const rows = [
      ["Image", q.id], ["Model", q.candidate.model], ["Run", `#${q.candidate.run_id}`],
      ["Replacement key", `${q.key32 || "none"} (${q.key_status})`],
      ["Tried before", q.models_tried.length ? q.models_tried.join(", ") : "nothing"],
    ];
    if (q.notes) rows.push(["Notes", q.notes]);
    $("rvInfo").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("rvPrev").disabled = this.idx === 0;
    $("rvNext").disabled = this.idx >= this.queue.length - 1;
    $("rvStrip").innerHTML = this.queue.map((x, i) =>
      `<img loading="lazy" data-i="${i}" class="${i === this.idx ? "cur" : ""}" src="/cthumb/${esc(x.candidate.path)}" title="${esc(x.label)}">`).join("");
    $("rvStrip").querySelector(".cur")?.scrollIntoView({ block: "nearest", inline: "center" });
  },

  go(d) {
    const n = this.idx + d;
    if (n >= 0 && n < this.queue.length) { this.idx = n; this.renderSingle(); }
  },

  async decide(action) {
    const q = this.queue[this.idx];
    if (!q) return;
    const r = await this.post(action, [q.id]);
    if (r.ok.length) {
      this.queue.splice(this.idx, 1);
      this.idx = Math.min(this.idx, Math.max(0, this.queue.length - 1));
      toast(action === "approve" ? `Approved ${q.label}` : `Rejected ${q.label}; it is back in the library to try another model`, action === "approve" ? "ok" : "");
      this.render();
    }
  },

  async post(action, ids) {
    try {
      const r = await api(`/api/review/${action}`, { item_ids: ids, delete_file: $("rvDelete").checked });
      const errs = Object.entries(r.errors);
      if (errs.length) toast(`${errs.length} could not be ${action}d: ${errs[0][1]}`, "bad");
      S.version = null;                                   // refresh library badges/counts
      return r;
    } catch (e) { toast(e.message, "bad"); return { ok: [], errors: {} }; }
  },

  key(e) {
    if (!$("modal").classList.contains("hidden")) { if (e.key === "Escape") this.closeDetail(); return; }
    if (S.tab !== "review" || this.mode !== "single" || !this.queue.length) return;
    if (e.target.closest("input, select, textarea") || e.ctrlKey || e.altKey || e.metaKey) return;
    const k = e.key.toLowerCase();
    const acts = { a: () => this.decide("approve"), r: () => this.decide("reject"), arrowleft: () => this.go(-1),
                   arrowright: () => this.go(1), " ": () => this.viewer.flip(), f: () => this.viewer.fit(),
                   "1": () => this.viewer.oneToOne(), p: () => $("rvPixel").click() };
    if (acts[k]) { e.preventDefault(); acts[k](); }
  },

  // ---------- grid ----------
  renderGrid() {
    $("rvCards").innerHTML = this.queue.map((q, i) => `
      <div class="card-item${this.selected.has(q.id) ? " sel" : ""}" data-i="${i}" title="${esc(q.id)}">
        <span class="check"></span>
        <div class="hov">
          <img class="o" loading="lazy" src="/ws/${esc(q.thumb)}" alt="">
          <img class="u" loading="lazy" src="/cthumb/${esc(q.candidate.path)}" alt="">
          <div class="line"></div>
        </div>
        <div class="lbl">${esc(q.label)}</div>
        <div class="meta"><span class="tag">${esc(q.candidate.model)}</span></div>
      </div>`).join("");
    this.updateBulk();
  },

  gridClick(e) {
    const c = e.target.closest(".card-item"); if (!c) return;
    const id = this.queue[+c.dataset.i].id;
    this.selected.has(id) ? this.selected.delete(id) : this.selected.add(id);
    c.classList.toggle("sel");
    this.updateBulk();
  },

  updateBulk() {
    $("rvSelCount").textContent = this.selected.size;
    $("rvBulkApprove").disabled = $("rvBulkReject").disabled = !this.selected.size;
  },

  async bulk(action) {
    const ids = [...this.selected];
    if (action === "reject" && !confirm(`Reject ${ids.length} image(s)? They go back to the library to try another model.` +
        ($("rvDelete").checked ? " Their upscaled files are deleted." : ""))) return;
    const r = await this.post(action, ids);
    toast(`${r.ok.length} image(s) ${action}d`, action === "approve" ? "ok" : "");
    r.ok.forEach((id) => this.selected.delete(id));
    await this.load();
  },

  // ---------- notifications ----------
  askPermission() {
    if ($("optNotify").checked && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
  },

  runDone(ev) {
    const waiting = +$("reviewCount").textContent || 0;
    const text = `Run ${ev.run_id} ${ev.state === "done" ? "finished" : ev.state}: ${ev.done} upscaled` +
                 (ev.failed ? `, ${ev.failed} failed` : "") + ". Ready for review.";
    if (ev.done) {
      $("bannerText").textContent = text;
      $("banner").classList.remove("hidden");
    }
    if ($("optNotify").checked && "Notification" in window && Notification.permission === "granted") {
      const n = new Notification("DC2 Upscale Manager", { body: text });
      n.onclick = () => { window.focus(); showTab("review"); n.close(); };
    }
    if ($("optChime").checked) this.chime();
    if (S.tab === "review") this.load();
    return waiting;
  },

  chime() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [[660, 0], [880, 0.18]].forEach(([f, t]) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f; o.type = "sine";
        g.gain.setValueAtTime(0.0001, ctx.currentTime + t);
        g.gain.exponentialRampToValueAtTime(0.25, ctx.currentTime + t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + t + 0.35);
        o.connect(g).connect(ctx.destination);
        o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.4);
      });
      setTimeout(() => ctx.close(), 1000);
    } catch (e) { /* audio not available */ }
  },

  // ---------- detail popup (library double-click) ----------
  async openDetail(id) {
    const it = S.byId.get(id);
    if (!it) return;
    let right = null, rightLabel = "", cand = null;
    if (it.status === "approved" && it.approved_png) {
      right = `/ws/${it.approved_png}?v=${Date.now()}`; rightLabel = "Approved";
    } else if (it.status === "review") {
      cand = (await api("/api/review")).find((q) => q.id === id)?.candidate;
      if (cand) { right = `/ws/${cand.path}`; rightLabel = `Upscaled · ${cand.model}`; }
    }
    if (!right) { toast(`${it.label}: nothing to compare yet (status: ${it.status}). Upscale it first.`); return; }
    this.detail = it;
    $("mdTitle").textContent = it.label;
    $("mdSub").textContent = ` ${it.id} · ${it.status}`;
    $("mdMsg").textContent = "";
    $("mdReplace").classList.toggle("hidden", it.status !== "approved");
    $("mdReopen").classList.toggle("hidden", it.status !== "approved");
    $("mdApprove").classList.toggle("hidden", it.status !== "review");
    $("mdReject").classList.toggle("hidden", it.status !== "review");
    $("modal").classList.remove("hidden");
    if (!this.mdViewer) this.mdViewer = new Compare($("mdViewer"));
    this.mdViewer.setPixelated($("rvPixel").checked);
    this.mdViewer.load(`/ws/${it.original_png}`, right, "Original 320×240", rightLabel);
  },

  closeDetail() { $("modal").classList.add("hidden"); this.detail = null; },

  async replaceFile() {
    const f = $("mdFile").files[0];
    $("mdFile").value = "";
    if (!f || !this.detail) return;
    const fd = new FormData();
    fd.append("file", f);
    const msg = $("mdMsg");
    try {
      const res = await fetch(`/api/items/replace?id=${encodeURIComponent(this.detail.id)}`, { method: "POST", body: fd });
      const r = await res.json();
      if (!res.ok) throw new Error(r.error);
      msg.className = "msg ok"; msg.textContent = `Now using ${f.name}.`;
      this.mdViewer.load(`/ws/${this.detail.original_png}`, `/ws/${r.approved_png}?v=${Date.now()}`, "Original 320×240", "Approved (edited)");
      S.version = null;
    } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
  },

  async reopen() {
    if (!this.detail || !confirm(`Send "${this.detail.label}" back to "to do"? Its approved image will no longer be used.`)) return;
    await this.post("reopen", [this.detail.id]);
    toast(`${this.detail.label} reopened`);
    this.closeDetail();
  },

  async detailDecide(action) {
    if (!this.detail) return;
    const r = await this.post(action, [this.detail.id]);
    if (r.ok.length) { toast(`${this.detail.label} ${action}d`, action === "approve" ? "ok" : ""); this.closeDetail(); if (S.tab === "review") this.load(); }
  },
};

Review.init();
if (S.tab === "review") Review.load();        // page opened directly on #review (app.js ran first)
