"""Extract every replaceable background from a Dino Crisis 2 install into the workspace.

Read-only on the game folder. Produces, per item:
  originals/<kind>/<name>.png   320x240 source image for upscaling
  thumbs/<kind>/<name>.jpg      160x120 preview
and a catalog row with the GOG-wrapper replacement keys.

Full-screen images (title, menus, warnings...): every 320x240 BGR555 picture the game shows as a
background, i.e. raw .PXL files and LZSS (type 5) chunks of .DAT files that decode to exactly
153,600 bytes into one of the two background buffers. Rendered exactly as the game draws them
(5-bit value << 3). Keys are computed with the game's own conversion (see core/dc2/gogtex.py).

Room backgrounds: the baseline JPEG in slot 1 of every camera group of every .DBS file, decoded with
Pillow (full 8-bit precision, up to ~3% brighter than the game's 15-bit display). Keys are looked up
in core/data/room_keys.json (predicted, unverified in game).
"""
import hashlib
import io
import json
import os
import zlib

import numpy as np
from PIL import Image

from .catalog import Catalog
from .dc2 import dc2codec as C
from .dc2 import dc2pack as P
from .dc2 import gogtex
from .install import installed_names
from .keycheck import SETTING as KEYCHECK_SETTING, adjust_keys
from .paths import check_game_dir, check_writable_target

BG_SIZE = 320 * 240 * 2
BG_BUFFERS = (0x600000, 0x6C1500)          # title buffer, general background buffer
VERIFIED_KEYS = {"ED666D62"}               # replacements confirmed working in game (TITLE, 32-bit)
THUMB_SIZE = (160, 120)
ROOM_KEYS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "room_keys.json")

FRIENDLY = {
    "TITLE.DAT": "Title screen / main menu",
    "TITLE2.DAT": "Title screen (after game clear)",
    "CAPLOGO.PXL": "Capcom logo",
    "M_TITLE.PXL": "Extra Crisis title",
    "M_SEL0.PXL": "Extra Crisis player select",
    "M_SEL1.PXL": "Extra Crisis 1P select",
    "M_SEL2.PXL": "Extra Crisis 2P select",
    "M_RES00.PXL": "Extra Crisis results",
    "EX_GET.PXL": "Bonus awards",
    "NO_DATA.PXL": "No data",
    "WARNINGE.PXL": "Warning screen (English)",
    "WARNINGF.PXL": "Warning screen (French)",
    "WARNINGG.PXL": "Warning screen (German)",
    "WARNINGI.PXL": "Warning screen (Italian)",
    "WARNINGJ.PXL": "Warning screen (Japanese)",
    "WARNINGS.PXL": "Warning screen (Spanish)",
    "CONTINUE.DAT": "Continue screen",
    "GAMEOVER.DAT": "Game over screen",
    "CAPLOGO.DAT": "Capcom logo",
    "ENTRY.DAT": "Player Entry (Extinct Points shop)",
    "RES01.DAT": "Results screen 1",
    "RES02.DAT": "Results screen 2",
    "WARNING.DAT": "Warning screen (shared by F/G/I/S)",
}


def _noop(done, total, msg):
    pass


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def bgr555_as_drawn(raw):
    """320x240 PS1 BGR555 -> RGB uint8 array, as the game draws it (v << 3, STP bit ignored)."""
    p = np.frombuffer(raw[:BG_SIZE], dtype="<u2").reshape(240, 320)
    rgb = np.dstack([(p & 31), (p >> 5) & 31, (p >> 10) & 31]).astype(np.uint8) << 3
    return rgb


def is_placeholder(rgb):
    """A blank filler image: every channel is (almost) constant."""
    return bool(rgb.reshape(-1, 3).std(axis=0).max() < 2.0)


def find_fullscreen(data_dir, log=print, ignore=()):
    """Yield dicts for every full-screen background in Data\\ (PXL files and DAT type-5 chunks).
    `ignore`: upper-case file names to skip (replacement files this tool installed into the game)."""
    names = sorted(os.listdir(data_dir))
    for name in names:
        path = os.path.join(data_dir, name)
        up = name.upper()
        if up in ignore:
            continue
        if up.endswith(".PXL"):
            raw = _read(path)
            if len(raw) != BG_SIZE:
                log(f"skip {name}: {len(raw)} bytes, not a 320x240 picture")
                continue
            yield {"id": up, "source_file": up, "slot": None, "raw": raw}
        elif up.endswith(".DAT") and not up.startswith("ST"):
            data = _read(path)
            try:
                groups = P.parse(data, "DAT")
            except P.ContainerError:
                continue                       # not a package (e.g. GOG's own replacement files)
            for e in groups[0]["entries"]:
                if e["type"] != 5 or e["p1"] not in BG_BUFFERS:
                    continue
                payload = data[e["offset"]:e["offset"] + e["size"]]
                raw = C.lz_decompress(payload)
                if len(raw) == BG_SIZE:
                    yield {"id": f"{up}#{e['slot']}", "source_file": up, "slot": e["slot"], "raw": raw}


def find_rooms(data_dir, log=print):
    """Yield dicts for every camera background (JPEG) in the .DBS files."""
    for name in sorted(os.listdir(data_dir)):
        up = name.upper()
        if not up.endswith(".DBS"):
            continue
        data = _read(os.path.join(data_dir, name))
        try:
            groups = P.parse(data, "DBS")
        except P.ContainerError as exc:
            log(f"skip {name}: {exc}")
            continue
        stage = up[:-4]
        for gi, g in enumerate(groups):
            ents = {e["slot"]: e for e in g["entries"]}
            e = ents.get(1)
            if e is None:
                log(f"skip {name} group {gi}: no slot 1")
                continue
            jpeg = data[e["offset"]:e["offset"] + e["size"]]
            if jpeg[:2] != b"\xff\xd8":
                log(f"skip {name} group {gi}: slot 1 is not a JPEG")
                continue
            yield {"id": f"{up}#{gi}", "source_file": up, "grp": gi, "slot": 1, "stage": stage,
                   "camera": gi, "jpeg": jpeg}


def _safe(item_id):
    return item_id.replace("#", "_").replace(".", "_")


def _image_paths(kind, item_id):
    """Workspace-relative paths (forward slashes) of an item's original PNG and thumbnail."""
    return (f"originals/{kind}/{_safe(item_id)}.png", f"thumbs/{kind}/{_safe(item_id)}.jpg")


def _write_images(ws, rel_png, rel_thumb, rgb, force):
    png, thumb = os.path.join(ws, rel_png), os.path.join(ws, rel_thumb)
    if force or not (os.path.isfile(png) and os.path.isfile(thumb)):
        os.makedirs(os.path.dirname(png), exist_ok=True)
        os.makedirs(os.path.dirname(thumb), exist_ok=True)
        im = Image.fromarray(rgb, "RGB")
        im.save(png)
        im.resize(THUMB_SIZE, Image.LANCZOS).save(thumb, quality=85)


def extract(game_dir, workspace, progress=_noop, log=print):
    """Run a full (re-runnable) extraction. Returns a summary dict."""
    warnings = check_game_dir(game_dir)
    check_writable_target(workspace, game_dir)
    for w in warnings:
        log("WARNING: " + w)
    data_dir = os.path.join(game_dir, "Data")
    cat = Catalog(workspace)
    cat.set_setting("game_dir", os.path.abspath(game_dir))
    cat.set_setting("exe_warnings", warnings)
    with open(ROOM_KEYS, encoding="utf-8") as fh:
        room_keys = json.load(fh)["keys"]
    keycheck = cat.get_setting(KEYCHECK_SETTING, {}) or {}     # outcome of an in-game key check, if any
    summary = {"fullscreen": 0, "rooms": 0, "duplicates": 0, "placeholders": 0,
               "new": 0, "changed": 0, "unchanged": 0, "no_key": 0, "warnings": warnings}
    seen = {}                                           # content sha1 -> first item id

    progress(0, 0, "scanning full-screen images")
    fullscreen = list(find_fullscreen(data_dir, log, ignore=installed_names(game_dir)))
    progress(0, 0, "scanning room backgrounds")
    rooms = list(find_rooms(data_dir, log))
    total = len(fullscreen) + len(rooms)
    done = 0

    for f in fullscreen:
        sha = hashlib.sha1(f["raw"]).hexdigest()
        rgb = bgr555_as_drawn(f["raw"])
        k32 = "%08X" % gogtex.bg_crc(f["raw"], 32)
        k16 = "%08X" % gogtex.bg_crc(f["raw"], 16)
        name = f["source_file"]
        label = FRIENDLY.get(name, name)
        if f["slot"] is not None:
            label += f" ({name} slot {f['slot']})"
        item = {"id": f["id"], "kind": "fullscreen", "label": label, "source_file": name,
                "slot": f["slot"], "content_sha1": sha, "key32": k32, "key16": k16,
                "key_status": "verified" if k32 in VERIFIED_KEYS else "computed",
                "dup_of": seen.get(sha), "placeholder": int(is_placeholder(rgb))}
        seen.setdefault(sha, f["id"])
        item["original_png"], item["thumb"] = _image_paths("fullscreen", f["id"])
        result = cat.upsert_item(adjust_keys(item, keycheck))
        _write_images(workspace, item["original_png"], item["thumb"], rgb, force=result != "unchanged")
        summary[result] += 1
        summary["fullscreen"] += 1
        summary["duplicates"] += item["dup_of"] is not None
        summary["placeholders"] += item["placeholder"]
        done += 1
        progress(done, total, f["id"])

    for r in rooms:
        sha = hashlib.sha1(r["jpeg"]).hexdigest()
        keys = room_keys.get("%08X" % zlib.crc32(r["jpeg"]))
        rgb = np.asarray(Image.open(io.BytesIO(r["jpeg"])).convert("RGB"))
        item = {"id": r["id"], "kind": "room", "label": f"{r['stage']} cam {r['camera']:02d}",
                "source_file": r["source_file"], "grp": r["grp"], "slot": r["slot"], "stage": r["stage"],
                "camera": r["camera"], "content_sha1": sha,
                "key32": keys and keys["key32"], "key16": keys and keys["key16"],
                "key32_alt": keys and keys["key32_alt"], "key16_alt": keys and keys["key16_alt"],
                "key_status": "predicted" if keys else "unknown",
                "dup_of": seen.get(sha), "placeholder": int(is_placeholder(rgb))}
        seen.setdefault(sha, r["id"])
        item["original_png"], item["thumb"] = _image_paths("rooms", r["id"])
        result = cat.upsert_item(adjust_keys(item, keycheck))
        _write_images(workspace, item["original_png"], item["thumb"], rgb, force=result != "unchanged")
        summary[result] += 1
        summary["rooms"] += 1
        summary["duplicates"] += item["dup_of"] is not None
        summary["placeholders"] += item["placeholder"]
        summary["no_key"] += keys is None
        done += 1
        progress(done, total, r["id"])

    cat.set_setting("last_extract", summary)
    cat.close()
    return summary
