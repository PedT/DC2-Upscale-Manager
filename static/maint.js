// Setup tab, M6: workspace disk usage and clean-up. Uses helpers from app.js: $, S, api, esc, toast.
"use strict";

const Maint = {
  LABELS: {
    rejected: ["Rejected upscales", "kept on disk because \"delete rejected files\" was unticked"],
    approved_runs: ["Run copies of approved upscales", "approved\\ keeps its own copy"],
    run_inputs: ["Input links of finished runs", "hard links to the originals; resume does not need them"],
    previews: ["Preview cache", "small review previews, rebuilt when needed"],
    staging: ["Install staging", "rebuilt by every install"],
  },

  init() {
    $("dkRefresh").onclick = () => this.load();
    $("dkClean").onclick = () => this.clean();
    $("dkCats").addEventListener("change", () => this.updateButton());
  },

  fmt(b) {
    if (b >= 1073741824) return `${(b / 1073741824).toFixed(2)} GB`;
    if (b >= 1048576) return `${(b / 1048576).toFixed(1)} MB`;
    return `${Math.round(b / 1024)} KB`;
  },

  async load() {
    const d = await api("/api/disk");
    $("dkPath").textContent = `${d.workspace} · ${this.fmt(d.total_bytes)} in total`;
    $("dkAreas").innerHTML = d.areas.map((a) => `<tr><td>${esc(a.area)}</td><td>${a.files}</td><td>${this.fmt(a.bytes)}</td></tr>`).join("");
    const keep = new Set([...document.querySelectorAll("#dkCats input:checked")].map((i) => i.value));
    const first = !$("dkCats").children.length;
    $("dkCats").innerHTML = Object.entries(d.cleanable).map(([k, v]) => {
      const [label, why] = this.LABELS[k];
      const checked = (first ? k !== "run_inputs" : keep.has(k)) && v.files ? "checked" : "";
      return `<label class="chk dk"><input type="checkbox" value="${k}" ${checked} ${v.files ? "" : "disabled"}>
        <b>${esc(label)}</b>&nbsp;<span class="muted">${v.files} file(s), ${this.fmt(v.bytes)} · ${esc(why)}</span></label>`;
    }).join("");
    this.busy = d.busy;
    this.updateButton();
  },

  selected() { return [...document.querySelectorAll("#dkCats input:checked")].map((i) => i.value); },

  updateButton() {
    $("dkClean").disabled = !this.selected().length || this.busy;
    $("dkClean").title = this.busy ? "wait until the running job finishes" : "";
  },

  async clean() {
    const cats = this.selected();
    if (!confirm(`Delete the selected files (${cats.map((c) => this.LABELS[c][0]).join(", ")})?`)) return;
    try {
      const r = await api("/api/cleanup", { categories: cats });
      const files = Object.values(r.freed).reduce((a, v) => a + v.files, 0);
      const bytes = Object.values(r.freed).reduce((a, v) => a + v.bytes, 0);
      $("dkMsg").className = "msg ok";
      $("dkMsg").textContent = `Removed ${files} file(s), freed ${this.fmt(bytes)}.`;
      this.load();
    } catch (e) { $("dkMsg").className = "msg bad"; $("dkMsg").textContent = e.message; }
  },
};

Maint.init();
if (S.tab === "setup") Maint.load();
