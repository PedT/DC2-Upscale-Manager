// Before/after comparison viewer with a draggable divider (original left, upscaled right),
// mouse-wheel zoom around the cursor, drag-to-pan when zoomed, and synchronized images.
"use strict";

class Compare {
  constructor(el) {
    this.el = el;
    el.classList.add("cmp");
    el.innerHTML = `
      <div class="cmp-layer cmp-base"><img class="cmp-orig" alt="original" draggable="false"></div>
      <div class="cmp-layer cmp-top"><img class="cmp-up" alt="upscaled" draggable="false"></div>
      <div class="cmp-divider"><div class="cmp-knob">&#9664;&#9654;</div></div>
      <div class="cmp-label cmp-label-l">Original</div>
      <div class="cmp-label cmp-label-r">Upscaled</div>
      <div class="cmp-zoom"></div>`;
    this.orig = el.querySelector(".cmp-orig");
    this.up = el.querySelector(".cmp-up");
    this.top = el.querySelector(".cmp-top");
    this.divider = el.querySelector(".cmp-divider");
    this.zoomLabel = el.querySelector(".cmp-zoom");
    this.split = 0.5; this.zoom = 1; this.panX = 0; this.panY = 0;
    this.nat = { w: 1280, h: 960 };
    this.savedSplit = null;
    this.up.addEventListener("load", () => { this.nat = { w: this.up.naturalWidth, h: this.up.naturalHeight }; this.layout(); });
    new ResizeObserver(() => this.layout()).observe(el);
    this._bind();
  }

  load(origSrc, upSrc, leftLabel = "Original", rightLabel = "Upscaled") {
    this.el.querySelector(".cmp-label-l").textContent = leftLabel;
    this.el.querySelector(".cmp-label-r").textContent = rightLabel;
    this.orig.src = origSrc;
    this.up.src = upSrc;
    this.fit();
  }

  setPixelated(on) { this.orig.style.imageRendering = on ? "pixelated" : "auto"; }
  fit() { this.zoom = 1; this.panX = 0; this.panY = 0; this.layout(); }
  oneToOne() { this.zoom = 1 / this._fitScale(); this.panX = 0; this.panY = 0; this.layout(); }
  zoomBy(f) { const r = this.el.getBoundingClientRect(); this._zoomAt(f, r.width / 2, r.height / 2); }
  flip() {
    if (this.savedSplit == null) { this.savedSplit = this.split; this.split = this.split > 0.5 ? 0 : 1; }
    else { this.split = this.savedSplit; this.savedSplit = null; }
    this.layout();
  }

  _fitScale() {
    const W = this.el.clientWidth || 1, H = this.el.clientHeight || 1;
    return Math.min(W / this.nat.w, H / this.nat.h);
  }

  layout() {
    const W = this.el.clientWidth, H = this.el.clientHeight;
    if (!W || !H) return;
    const s = this._fitScale() * this.zoom;
    const w = this.nat.w * s, h = this.nat.h * s;
    const x = (W - w) / 2 + this.panX, y = (H - h) / 2 + this.panY;
    for (const img of [this.orig, this.up]) {
      img.style.width = `${w}px`; img.style.height = `${h}px`;
      img.style.left = `${x}px`; img.style.top = `${y}px`;
    }
    const sx = this.split * W;
    this.top.style.clipPath = `inset(0 0 0 ${sx}px)`;
    this.divider.style.left = `${sx}px`;
    this.zoomLabel.textContent = `${Math.round(s * 100)}% of upscaled`;
  }

  _zoomAt(factor, cx, cy) {
    const W = this.el.clientWidth, H = this.el.clientHeight;
    const old = this.zoom;
    this.zoom = Math.min(Math.max(this.zoom * factor, 1), 16 / this._fitScale());
    const k = this.zoom / old;
    // keep the point under the cursor fixed: offset from image centre scales by k
    const cxImg = W / 2 + this.panX, cyImg = H / 2 + this.panY;
    this.panX = cx - (cx - cxImg) * k - W / 2;
    this.panY = cy - (cy - cyImg) * k - H / 2;
    if (this.zoom === 1) { this.panX = 0; this.panY = 0; }
    this.layout();
  }

  _bind() {
    let mode = null, startX = 0, startY = 0, panX0 = 0, panY0 = 0;
    const pos = (e) => { const r = this.el.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
    this.el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      const [x, y] = pos(e);
      const onDivider = e.target.closest(".cmp-divider");
      mode = onDivider || this.zoom <= 1.0001 ? "split" : "pan";
      if (mode === "split") { this.split = Math.min(Math.max(x / this.el.clientWidth, 0), 1); this.savedSplit = null; }
      startX = x; startY = y; panX0 = this.panX; panY0 = this.panY;
      this.el.setPointerCapture(e.pointerId);
      this.el.classList.toggle("panning", mode === "pan");
      this.layout();
    });
    this.el.addEventListener("pointermove", (e) => {
      if (!mode) return;
      const [x, y] = pos(e);
      if (mode === "split") this.split = Math.min(Math.max(x / this.el.clientWidth, 0), 1);
      else { this.panX = panX0 + (x - startX); this.panY = panY0 + (y - startY); }
      this.layout();
    });
    const end = () => { mode = null; this.el.classList.remove("panning"); };
    this.el.addEventListener("pointerup", end);
    this.el.addEventListener("pointercancel", end);
    this.el.addEventListener("wheel", (e) => {
      e.preventDefault();
      const [x, y] = pos(e);
      this._zoomAt(e.deltaY < 0 ? 1.25 : 0.8, x, y);
    }, { passive: false });
    this.el.addEventListener("dblclick", () => this.fit());
  }
}
