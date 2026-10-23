"""Install approved HD images as GOG-wrapper replacement files.

The install target is the game folder itself (default) or a separate copy of the game. Install only ever
adds new XXXXXXXX.dat files to <target>\\Data\\ and keeps a record of them in
<target>\\dc2_upscaler_installed.json. It never changes the game's own files, never overwrites a file it
did not create (GOG's own replacement files included), and Uninstall removes exactly the recorded files and
then the record itself, leaving the folder as it was. The record travels with the game folder, so it
also tells the extraction which files in Data\\ to ignore.

For every approved item whose replacement key is trusted (key_status 'verified' or 'computed'):
  approved PNG -> Lanczos resize to a power-of-two canvas (1280x960 -> 2048x1024; the wrapper's D3DX
  loader would otherwise resample it with a softer filter) -> PNG bytes -> additive cipher ->
  <target>\\Data\\<key32>.dat  (32-bit colour mode, the game's normal setting), plus <key16>.dat for
  16-bit colour mode only when include_16bit is on (off by default: it doubles the background files).
Rooms also get their HD mask texture (see masks.py).

Install is a sync: it adds/updates files for approved items and removes files it installed earlier for
items that are no longer approved.
"""
import hashlib
import io
import json
import os
import re
import shutil
import time

from PIL import Image

from .dc2 import gogtex
from .masks import MaskSource, hd_atlas
from .paths import GOG_ENGLISH_EXE_SHA1, PathError, check_install_target

TRUSTED_KEYS = ("verified", "computed")
GOG_OWN_FILES = {"9A8B79FC.DAT", "C49FC969.DAT"}
MAX_SIDE = 4096
RECORD = "dc2_upscaler_installed.json"         # in the install target folder
LEGACY_MANIFEST = "install_manifest.json"      # older versions kept the record in the workspace
KEY_FILE_RE = re.compile(r"^[0-9A-F]{8}\.dat$", re.I)
SAVE_EVERY = 20


def next_pow2(n):
    p = 1
    while p < n:
        p <<= 1
    return p


def build_payload(png_path):
    """Approved image -> power-of-two PNG bytes (Lanczos). Returns (bytes, (w, h))."""
    with Image.open(png_path) as im:
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        im = im.convert("RGBA" if has_alpha else "RGB")
        w, h = im.size
        tw, th = min(next_pow2(w), MAX_SIDE), min(next_pow2(h), MAX_SIDE)
        if (tw, th) != (w, h):
            im = im.resize((tw, th), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "PNG")
    return buf.getvalue(), (tw, th)


def _sha1_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def game_color_depth(folder):
    """Colour depth the game is set to (dino2.cfg, u16 at offset 8): 32, 16, or None if unknown."""
    path = os.path.join(folder, "dino2.cfg") if folder else None
    try:
        with open(path, "rb") as fh:
            data = fh.read(10)
    except (OSError, TypeError):
        return None
    depth = int.from_bytes(data[8:10], "little") if len(data) >= 10 else None
    return depth if depth in (16, 32) else None


def read_record(folder):
    """The install record of a game folder, or None."""
    path = os.path.join(folder, RECORD) if folder else None
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def installed_names(folder):
    """Upper-case names of the Data\\ files this tool installed into a game folder (for the extraction)."""
    rec = read_record(folder)
    return {name.upper() for name in (rec or {}).get("files", {})}


class Installer:
    def __init__(self, catalog, mod_dir, game_dir=None, log=print, include_16bit=False):
        self.cat = catalog
        # The game normally runs in 32-bit colour; 16-bit mode uses different keys (a second file per image).
        self.include_16bit = include_16bit
        self.ws = catalog.workspace
        self.mod_dir = mod_dir
        self.game_dir = game_dir
        self.data_dir = os.path.join(mod_dir, "Data") if mod_dir else None
        self.record_path = os.path.join(mod_dir, RECORD) if mod_dir else None
        self.log = log

    # -- checks --
    def target_kind(self):
        return check_install_target(self.mod_dir, self.game_dir)

    def check_target(self):
        """Raise PathError if the install target is unusable; return a list of warnings otherwise."""
        if not self.mod_dir:
            raise PathError("no install target configured (Setup tab)")
        self.target_kind()
        if not os.path.isdir(self.data_dir) or not os.path.isfile(os.path.join(self.mod_dir, "Dino2.exe")):
            raise PathError(f"not a game folder (needs Dino2.exe and Data\\): {self.mod_dir}")
        if _sha1_file(os.path.join(self.mod_dir, "Dino2.exe")) != GOG_ENGLISH_EXE_SHA1:
            return ["the install target's Dino2.exe is not the GOG English build; the replacement keys may not match"]
        return []

    def classify(self):
        """Split approved items into installable ones and blocked ones (with a reason)."""
        ok, blocked = [], []
        for it in self.cat.items(status="approved"):
            if it["dup_of"]:
                continue                                        # covered by its master (same key)
            if it["key_status"] not in TRUSTED_KEYS or not it["key32"]:
                blocked.append((it, "replacement key not confirmed yet (run the in-game key check)"))
            elif it["kind"] == "room" and not (self.game_dir and os.path.isfile(
                    os.path.join(self.game_dir, "Data", it["source_file"]))):
                blocked.append((it, "the game folder is needed to build this room's HD masks (Setup tab)"))
            elif not it["approved_png"] or not os.path.isfile(os.path.join(self.ws, it["approved_png"])):
                blocked.append((it, "approved image file is missing"))
            else:
                ok.append(it)
        return ok, blocked

    # -- the record in the install target --
    def _migrate_legacy(self):
        """Move an old workspace manifest into the folder it describes (once)."""
        legacy = os.path.join(self.ws, LEGACY_MANIFEST)
        if not os.path.isfile(legacy):
            return
        with open(legacy, encoding="utf-8") as fh:
            old = json.load(fh)
        folder = old.get("mod_dir")
        if old.get("files") and folder and os.path.isdir(folder) and not os.path.isfile(os.path.join(folder, RECORD)):
            files = {n: {k: v for k, v in f.items() if k != "backup"} for n, f in old["files"].items()}
            self._write_record(os.path.join(folder, RECORD), {"files": files})
            self.log(f"moved the install record of {folder} into that folder")
        os.replace(legacy, legacy + ".migrated")

    def load_record(self):
        self._migrate_legacy()
        rec = read_record(self.mod_dir)
        return rec if rec else {"files": {}}

    def save_record(self, rec):
        """Write the record; with nothing installed the record file is removed, leaving the folder clean."""
        if rec["files"]:
            self._write_record(self.record_path, rec)
        elif os.path.isfile(self.record_path):
            os.remove(self.record_path)

    @staticmethod
    def _write_record(path, rec):
        body = {"tool": "DC2 Upscale Manager",
                "about": "Replacement files this tool added to Data\\. The game ignores this file; "
                         "Uninstall in the tool removes exactly these files and then this record.",
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "files": dict(sorted(rec["files"].items()))}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=1)
        os.replace(tmp, path)

    # -- actions --
    def install(self, progress=None):
        """Sync the install target with the approved, installable items. Returns a summary dict."""
        warnings = self.check_target()
        rec = self.load_record()
        ok, blocked = self.classify()
        summary = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "skipped_existing": [],
                   "blocked": [(it["id"], why) for it, why in blocked], "warnings": warnings, "items": 0,
                   "target": self.mod_dir, "target_is_game": self.target_kind() == "game",
                   "include_16bit": self.include_16bit}
        wanted = self._artifacts(ok, summary)                   # file name -> what to build
        stage = os.path.join(self.ws, "staging")
        os.makedirs(stage, exist_ok=True)
        payload_cache = {}
        for n, (name, art) in enumerate(sorted(wanted.items())):
            it = art["item"]
            if not KEY_FILE_RE.match(name) or name.upper() in GOG_OWN_FILES:
                raise PathError(f"refusing to write {name}")
            ck = (art["what"], it["id"])
            if ck not in payload_cache:
                payload_cache[ck] = self._build(art, summary)
            enc, size = payload_cache[ck]
            sha = hashlib.sha1(enc).hexdigest()
            dst = os.path.join(self.data_dir, name)
            ours = name in rec["files"]
            if os.path.isfile(dst):
                if _sha1_file(dst) == sha:
                    summary["unchanged"] += 1             # identical bytes can only be ours (e.g. after a crash)
                elif not ours:
                    summary["skipped_existing"].append(name)
                    self.log(f"{name} already exists in {self.data_dir} and was not installed by this tool; left as is")
                    continue
                else:
                    self._copy(enc, stage, name, dst)
                    summary["updated"] += 1
            else:
                self._copy(enc, stage, name, dst)
                summary["added"] += 1
            rec["files"][name] = {"item_id": it["id"], "label": it["label"], "what": art["what"], "sha1": sha,
                                  "size": len(enc), "texture": f"{size[0]}x{size[1]}", "installed": time.time()}
            if n % SAVE_EVERY == 0:
                self.save_record(rec)
            if progress:
                progress(n + 1, len(wanted), name)
        # files installed earlier for items that are no longer approved/installable
        for name in [n for n in rec["files"] if n not in wanted]:
            if self._remove(name, rec):
                summary["removed"] += 1
        self.save_record(rec)
        installed_ids = {f["item_id"] for f in rec["files"].values()}
        self._sync_flags(installed_ids)
        summary["items"] = len(installed_ids)
        summary["masks"] = sum(1 for name, a in wanted.items() if a["what"] == "mask" and name in rec["files"])
        return summary

    def _artifacts(self, ok, summary):
        """File name -> {item, what: background|mask, png, cam}. Rooms get an HD mask file per camera
        that has mask tiles, including duplicate cameras (same background, their own tiles)."""
        out = {}
        for it in ok:
            for key in ((it["key32"], it["key16"]) if self.include_16bit else (it["key32"],)):
                if key:
                    out[f"{key}.dat"] = {"item": it, "what": "background", "png": it["approved_png"]}
        rooms = {it["id"]: it for it in ok if it["kind"] == "room"}
        summary.update(overlay_rooms=[], fallback_tiles={})
        if not rooms:
            return out
        src = MaskSource(self.game_dir)
        cams = list(rooms.values()) + [d for d in self.cat.items(kind="room") if d["dup_of"] in rooms]
        for it in cams:
            master = rooms[it["dup_of"] or it["id"]]
            cam = src.camera(it["source_file"], it["grp"])
            if cam.kind == "overlay":
                summary["overlay_rooms"].append(it["label"])
            if cam.kind != "cells":
                continue
            name = f"{cam.key}.dat"
            if name in out and out[name]["png"] != master["approved_png"]:
                self.log(f"{it['id']}: mask key {cam.key} already used by {out[name]['item']['id']}; keeping the first")
                continue
            out.setdefault(name, {"item": it, "what": "mask", "png": master["approved_png"], "cam": cam})
        return out

    def _build(self, art, summary):
        """Encrypted replacement bytes + texture size for one artifact."""
        path = os.path.join(self.ws, art["png"])
        if art["what"] == "background":
            png, size = build_payload(path)
        else:
            with Image.open(path) as im:
                atlas, stats = hd_atlas(art["cam"], im)
            buf = io.BytesIO()
            atlas.save(buf, "PNG")
            png, size = buf.getvalue(), atlas.size
            if stats["fallback_cells"]:
                summary["fallback_tiles"][art["item"]["id"]] = stats["fallback_cells"]
        return gogtex.encrypt(png), size

    def uninstall(self):
        """Remove every recorded file, then the record. Files changed since install are left (and kept listed)."""
        rec = self.load_record()
        summary = {"removed": 0, "kept_modified": []}
        for name in list(rec["files"]):
            if self._remove(name, rec):
                summary["removed"] += 1
            else:
                summary["kept_modified"].append(name)
        if self.record_path:
            self.save_record(rec)
        self._sync_flags({f["item_id"] for f in rec["files"].values()})
        return summary

    def status(self):
        rec = self.load_record() if self.mod_dir else {"files": {}}
        ok, blocked = self.classify()
        try:
            kind = self.target_kind() if self.mod_dir else None
        except PathError:
            kind = None
        return {"mod_dir": self.mod_dir, "target_is_game": kind == "game", "record": self.record_path,
                "include_16bit": self.include_16bit, "color_depth": game_color_depth(self.mod_dir),
                "installed_files": rec["files"], "installable": [it["id"] for it in ok],
                "blocked": [{"id": it["id"], "label": it["label"], "reason": why} for it, why in blocked]}

    # -- helpers --
    def _copy(self, enc, stage, name, dst):
        staged = os.path.join(stage, name)
        with open(staged, "wb") as fh:
            fh.write(enc)
        shutil.copyfile(staged, dst)

    def _remove(self, name, rec):
        """Delete one recorded file (if unmodified). False if it was changed since install."""
        dst = os.path.join(self.data_dir, name)
        if os.path.isfile(dst) and _sha1_file(dst) != rec["files"][name]["sha1"]:
            self.log(f"{name} was changed outside this tool; leaving it in place")
            return False
        if os.path.isfile(dst):
            os.remove(dst)
        del rec["files"][name]
        return True

    def _sync_flags(self, installed_ids):
        for it in self.cat.items():
            if it["installed"] != (it["id"] in installed_ids):
                self.cat.set_installed(it["id"], it["id"] in installed_ids)
