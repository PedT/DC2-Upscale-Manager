// Install tab (M5): sync approved images into the install target, uninstall, show what is installed.
// Uses helpers from app.js: $, S, api, esc, toast.
"use strict";

const Install = {
  init() {
    $("in16bit").addEventListener("change", async (e) => {
      try {
        await api("/api/config", { install_16bit: e.target.checked });
        toast(e.target.checked ? "16-bit colour files will be installed: click Install to add them"
                               : "16-bit colour files will be left out: click Install to remove the ones already installed");
        this.load();
      } catch (err) { e.target.checked = !e.target.checked; toast(err.message, "bad"); }
    });
    $("kcImport").onclick = () => $("kcFile").click();
    $("kcFile").addEventListener("change", () => this.importLog());
    $("kcApply").onclick = () => this.applyReport();
    $("inInstall").onclick = async () => {
      try { await api("/api/install", {}); toast("Install queued"); } catch (e) { toast(e.message, "bad"); }
    };
    $("inUninstall").onclick = async () => {
      if (!confirm("Remove every file this tool installed into the install target (and its record file)?")) return;
      try { await api("/api/uninstall", {}); toast("Uninstall queued"); } catch (e) { toast(e.message, "bad"); }
    };
  },

  // ---------- in-game key check ----------
  STATUS: { verified: "seen in game", computed: "confirmed", predicted: "predicted", unknown: "no key" },

  async loadKeys() {
    const k = await api("/api/keycheck");
    const part = (kind, label) => {
      const c = k.counts[kind] || {};
      return `<div><b>${label}</b>${Object.entries(c).map(([s, n]) =>
        `<span class="badge key-${s}">${n} ${this.STATUS[s] || s}</span>`).join(" ")}</div>`;
    };
    $("kcCounts").innerHTML = part("fullscreen", "Menus") + part("room", "Rooms");
    const st = k.state || {};
    $("kcState").textContent = st.last_decision
      ? `Last check: ${st.last_message}${st.variant ? ` (room keys use the ${st.variant === "alt" ? "alternative" : "primary"} prediction)` : ""}`
      : "No in-game check imported yet.";
    if (k.last_report) this.showReport(k.last_report);
  },

  async importLog() {
    const f = $("kcFile").files[0];
    $("kcFile").value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const res = await fetch("/api/keycheck/analyze", { method: "POST", body: fd });
      const r = await res.json();
      if (!res.ok) throw new Error(r.error);
      this.showReport(r);
    } catch (e) { toast(e.message, "bad"); }
  },

  showReport(r) {
    const good = r.decision === "primary" || r.decision === "alt";
    const matchedIds = r.fullscreen_verified.length + Object.keys(r.rooms).length;
    const contra = Object.entries(r.contradicted);
    $("kcReport").classList.remove("hidden");
    $("kcReport").innerHTML = `
      <p class="msg ${good ? "ok" : r.decision === "inconclusive" ? "warn" : "bad"}">${esc(r.message)}</p>
      <ul class="muted small">
        <li>${esc(r.file || "log")}: ${r.events} events, ${r.probes} replacement lookups (${r.unique_probes} different keys, ${r.found_installed} found installed files)</li>
        <li>Menus seen: ${r.fullscreen_verified.length ? esc(r.fullscreen_verified.join(", ")) : "none"}</li>
        <li>Rooms seen: ${r.room_counts.primary} matched the primary prediction, ${r.room_counts.alt} the alternative,
            ${r.room_counts.both} where both agree (need ${r.min_discriminating} where they differ)</li>
        ${contra.length ? `<li>Cameras loaded without a matching key: ${contra.slice(0, 15).map(([id]) => esc(id)).join(", ")}${contra.length > 15 ? " …" : ""}</li>` : ""}
      </ul>`;
    const btn = $("kcApply");
    btn.classList.toggle("hidden", !matchedIds && !good);
    btn.textContent = good ? "Apply: confirm all room keys" : `Apply: mark the ${matchedIds} image(s) seen as verified`;
  },

  async applyReport() {
    try {
      const r = await api("/api/keycheck/apply", {});
      toast(`Key check applied: ${r.changed} image(s) updated, ${r.verified} seen in game` +
            (r.variant ? `, rooms use the ${r.variant === "alt" ? "alternative" : "primary"} prediction` : ""), "ok");
      $("kcApply").classList.add("hidden");
      $("kcReport").classList.add("hidden");
      S.version = null;
      this.load();
    } catch (e) { toast(e.message, "bad"); }
  },

  async load() {
    this.loadKeys().catch((e) => console.error(e));
    const st = await api("/api/install");
    $("inMod").textContent = st.mod_dir || "(not set)";
    $("inGameNote").classList.toggle("hidden", !st.target_is_game);
    $("in16bit").checked = !!st.include_16bit;
    const depth = st.color_depth;
    const mismatch = depth === 16 && !st.include_16bit;
    $("inDepth").className = `small ${mismatch ? "msg warn" : "muted"}`;
    $("inDepth").textContent = !depth ? "" : mismatch
      ? "The game is set to 16-bit colour: tick the box above and click Install, or the game will show the original backgrounds."
      : `The game is set to ${depth}-bit colour` + (depth === 32 && !st.include_16bit
        ? ", so the 16-bit files aren't needed." : ".");
    $("inError").textContent = st.error || "";
    $("inWarnings").innerHTML = (st.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
    const files = Object.entries(st.installed_files).sort((a, b) => a[1].label.localeCompare(b[1].label));
    const installedItems = new Set(files.map(([, f]) => f.item_id));
    const pending = st.installable.filter((id) => !installedItems.has(id)).length;
    $("inReady").textContent = st.installable.length;
    $("inInstalled").textContent = installedItems.size;
    $("inFiles").textContent = files.length;
    $("inBlocked").textContent = st.blocked.length;
    $("inInstall").disabled = !!st.error;
    $("inInstall").textContent = pending ? `Install / update game (${pending} new)` : "Install / update game";
    $("inUninstall").disabled = !files.length;
    $("inFilesBody").innerHTML = files.map(([name, f]) => `<tr>
        <td>${esc(f.label)} <span class="muted small">${esc(f.item_id)}</span></td>
        <td>${f.what === "mask" ? '<span class="tag" title="pieces drawn over characters behind objects">HD mask</span>' : "background"}</td>
        <td><code>Data\\${esc(name)}</code></td>
        <td>${esc(f.texture)}</td><td>${(f.size / 1048576).toFixed(1)} MB</td>
        <td>${new Date(f.installed * 1000).toLocaleString()}</td></tr>`).join("")
      || `<tr><td colspan="6" class="muted">Nothing installed yet.</td></tr>`;
    $("inBlockedCard").classList.toggle("hidden", !st.blocked.length);
    $("inBlockedList").innerHTML = st.blocked.slice(0, 50).map((b) => `<li>${esc(b.label)}: ${esc(b.reason)}</li>`).join("") +
      (st.blocked.length > 50 ? `<li>… and ${st.blocked.length - 50} more</li>` : "");
  },
};

Install.init();
if (S.tab === "install") Install.load();      // page opened directly on #install (app.js ran first)
