"""HD occlusion masks for room backgrounds.

How the game does it (Dino2.exe, verified against an in-game Process Monitor log for 84 cameras):
  * DBS slot 0: u16 count, u16 0, count x {u8 tx, u8 ty, s16 z}  - where each mask tile goes on screen
    (16x16 quad at (tx*16, (ty & 15)*16)) and its depth.
  * DBS slot 3 (type 0, LZSS): count x 18x18 BGR555 tiles (16x16 picture + 1 px border); bit 15 (STP)
    marks the pixels that cover characters standing behind them.
  * 0x4be770 paints tile i into a 256x768 staging surface at ((i%14)*18, (i%196//14)*18 + (i//196)*256)
    in A8R8G8B8 (r5<<19 | g5<<11 | b5<<3, alpha 0xFF000000 where STP is set), then copies page p
    (256x256) into a fresh 512x512 texture at ((p&1)*256, (p>>1)*256).
  * GOG's ddraw.dll looks up a replacement for that texture by CRC32 of its pixels after the last page
    copy -> mask_key().  A replacement may be any size; the game samples it with normalized UVs.

hd_atlas() rebuilds that 512x512 texture at 4x (2048x2048) from an approved HD background: every tile is
re-cut from the HD image at its screen position (so it matches the new art exactly), with a 4 px border
clamped at the image edge, and the tile's STP pattern scaled up as a soft alpha channel. Tiles whose
original picture does not match the original background (rare; the game sometimes uses masks cut from a
different state of the room) fall back to an enlarged copy of the original tile, and are reported.
"""
import collections
import io
import os
import struct
import zlib

import numpy as np
from PIL import Image

from .dc2 import dc2codec as C
from .dc2 import dc2pack as P

K = 4                       # HD scale: approved backgrounds are used at 1280x960
CELL = 18
PER_ROW, PER_PAGE = 14, 196
FALLBACK_DIFF = 40.0        # mean |tile - background| over opaque pixels (8-bit levels) that counts as "different"


class CameraMasks:
    def __init__(self, kind, cells=None, recs=None, jpeg=None):
        self.kind = kind                     # 'cells' | 'overlay' | 'none'
        self.cells = cells                   # (n, 18, 18) uint16
        self.recs = recs or []               # [(tx, ty, z)]
        self.jpeg = jpeg

    @property
    def count(self):
        return 0 if self.cells is None else len(self.cells)

    @property
    def pages(self):
        return (self.count + PER_PAGE - 1) // PER_PAGE

    @property
    def key(self):
        return mask_key(self.cells) if self.kind == "cells" else None


class MaskSource:
    """Reads mask data from the game's DBS files (read-only), caching recently used files."""

    def __init__(self, game_dir, cache_files=4):
        self.data_dir = os.path.join(game_dir, "Data")
        self._cache = collections.OrderedDict()
        self._max = cache_files

    def _file(self, dbs):
        dbs = dbs.upper()
        if dbs not in self._cache:
            with open(os.path.join(self.data_dir, dbs), "rb") as fh:
                data = fh.read()
            self._cache[dbs] = (data, P.parse(data, "DBS"))
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        self._cache.move_to_end(dbs)
        return self._cache[dbs]

    def camera(self, dbs, group):
        data, groups = self._file(dbs)
        ents = {e["slot"]: e for e in groups[group]["entries"]}

        def payload(slot):
            e = ents[slot]
            return data[e["offset"]:e["offset"] + e["size"]]

        jpeg = payload(1) if 1 in ents else None
        if 3 not in ents:
            return CameraMasks("none", jpeg=jpeg)
        if ents[3]["type"] != 0:
            return CameraMasks("overlay", jpeg=jpeg)          # type-8 sprite page (boat, fence...)
        raw = C.lz_decompress(payload(3))
        s0 = payload(0)
        count = struct.unpack_from("<H", s0, 0)[0] if len(s0) >= 4 else 0
        count = min(count, len(raw) // (CELL * CELL * 2), (len(s0) - 4) // 4)
        if count <= 0:
            return CameraMasks("none", jpeg=jpeg)
        cells = np.frombuffer(raw[:count * CELL * CELL * 2], dtype="<u2").reshape(count, CELL, CELL)
        recs = [struct.unpack_from("<BBh", s0, 4 + 4 * i) for i in range(count)]
        return CameraMasks("cells", cells, recs, jpeg)


def _place(i):
    """Top-left of tile i inside the 512x512 texture (game layout)."""
    page, j = divmod(i, PER_PAGE)
    return ((page & 1) * 256 + (j % PER_ROW) * CELL, (page >> 1) * 256 + (j // PER_ROW) * CELL)


def mask_key(cells):
    """GOG-wrapper key of a camera's mask texture (32-bit display mode)."""
    tex = np.zeros((512, 512), dtype="<u4")
    px = cells.astype(np.uint32)
    conv = (((px & 31) << 19) | (((px >> 5) & 31) << 11) | (((px >> 10) & 31) << 3)
            | (((px >> 15) & 1) * np.uint32(0xFF000000))).astype("<u4")
    for i in range(len(cells)):
        x, y = _place(i)
        tex[y:y + CELL, x:x + CELL] = conv[i]
    return "%08X" % zlib.crc32(tex.tobytes())


def _expand(cell):
    """18x18 BGR555 tile -> RGB uint8 (full-range 5->8 bit)."""
    c = cell.astype(np.uint16)
    rgb = np.dstack([c & 31, (c >> 5) & 31, (c >> 10) & 31]).astype(np.uint8)
    return (rgb << 3) | (rgb >> 2)


def hd_atlas(cam, hd_image, k=K):
    """Build the HD replacement for a camera's mask texture. Returns (RGBA PIL image, stats dict)."""
    if cam.kind != "cells":
        raise ValueError("camera has no mask tiles")
    hd = hd_image.convert("RGB")
    if hd.size != (320 * k, 240 * k):
        hd = hd.resize((320 * k, 240 * k), Image.LANCZOS)
    H = np.asarray(hd)
    pad = np.pad(H, ((k, k), (k, k), (0, 0)), mode="edge")
    orig = np.asarray(Image.open(io.BytesIO(cam.jpeg)).convert("RGB")).astype(np.int16) if cam.jpeg else None
    atlas = np.zeros((512 * k, 512 * k, 4), dtype=np.uint8)
    size = CELL * k
    fallback = 0
    for i, (tx, ty, _z) in enumerate(cam.recs):
        x0, y0 = tx * 16, (ty & 15) * 16
        cell = cam.cells[i]
        opaque = (cell & 0x8000) != 0
        rgb = _expand(cell)
        use_bg = True
        inner = opaque[1:17, 1:17]
        if orig is not None and inner.any():
            region = orig[y0:y0 + 16, x0:x0 + 16]
            if region.shape[:2] == (16, 16):
                diff = np.abs(rgb[1:17, 1:17].astype(np.int16) - region).mean(axis=2)[inner].mean()
                use_bg = diff <= FALLBACK_DIFF
        if use_bg:
            crop = pad[y0 * k:y0 * k + size, x0 * k:x0 * k + size]   # pad offset k = the 1-px border at 4x
        else:
            crop = np.asarray(Image.fromarray(rgb, "RGB").resize((size, size), Image.LANCZOS))
            fallback += 1
        alpha = np.asarray(Image.fromarray(opaque.astype(np.uint8) * 255, "L").resize((size, size), Image.BILINEAR))
        cx, cy = _place(i)
        atlas[cy * k:cy * k + size, cx * k:cx * k + size, :3] = crop
        atlas[cy * k:cy * k + size, cx * k:cx * k + size, 3] = alpha
    return Image.fromarray(atlas, "RGBA"), {"cells": cam.count, "pages": cam.pages, "fallback_cells": fallback}


def preview(cam, hd_image, atlas, k=K):
    """Debug view: the HD background darkened, with every mask tile drawn back where the game draws it."""
    hd = hd_image.convert("RGB")
    if hd.size != (320 * k, 240 * k):
        hd = hd.resize((320 * k, 240 * k), Image.LANCZOS)
    out = (np.asarray(hd).astype(np.float32) * 0.35).astype(np.uint8)
    A = np.asarray(atlas)
    for i, (tx, ty, _z) in enumerate(cam.recs):
        x0, y0 = tx * 16 * k, (ty & 15) * 16 * k
        cx, cy = _place(i)
        tile = A[(cy + 1) * k:(cy + 17) * k, (cx + 1) * k:(cx + 17) * k].astype(np.float32)
        a = tile[..., 3:4] / 255.0
        dst = out[y0:y0 + 16 * k, x0:x0 + 16 * k].astype(np.float32)
        out[y0:y0 + 16 * k, x0:x0 + 16 * k] = (tile[..., :3] * a + dst * (1 - a)).astype(np.uint8)
    return Image.fromarray(out, "RGB")
