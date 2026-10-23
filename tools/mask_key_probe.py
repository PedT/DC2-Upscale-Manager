"""Research helper: test mask-texture key hypotheses against keys observed in a ProcMon log."""
import json
import os
import sys
import zlib

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core.dc2 import dc2codec as C  # noqa: E402
from core.dc2 import dc2pack as P  # noqa: E402

GAME = r"C:\GOG Games\Dino Crisis 2\Data"


def cells_for(cam):
    dbs, gi = cam.split("#")
    data = open(os.path.join(GAME, dbs), "rb").read()
    g = P.parse(data, "DBS")[int(gi)]
    ents = {e["slot"]: e for e in g["entries"]}
    e3 = ents[3]
    raw = C.lz_decompress(data[e3["offset"]:e3["offset"] + e3["size"]])
    n = len(raw) // 648
    return np.frombuffer(raw[:n * 648], dtype="<u2").reshape(n, 18, 18)


def convert(px, fmt):
    r5, g5, b5 = px & 31, (px >> 5) & 31, (px >> 10) & 31
    stp = (px >> 15) & 1
    if fmt == "argb8888":
        return ((r5 << 19) | (g5 << 11) | (b5 << 3) | (stp * 0xFF000000)).astype("<u4")
    if fmt == "xrgb8888_noalpha":
        return ((r5 << 19) | (g5 << 11) | (b5 << 3)).astype("<u4")
    if fmt == "argb1555":
        return ((r5 << 10) | (g5 << 5) | b5 | (stp << 15)).astype("<u2")
    if fmt == "argb4444":
        return (((r5 >> 1) << 8) | ((g5 >> 1) << 4) | (b5 >> 1) | (stp * 0xF000)).astype("<u2")
    raise ValueError(fmt)


def build(cells, fmt):
    dtype = "<u4" if fmt.endswith("8888") or fmt == "xrgb8888_noalpha" else "<u2"
    staging = np.zeros((768, 256), dtype=dtype)
    for i, cell in enumerate(cells):
        page, k = divmod(i, 196)
        y = page * 256 + (k // 14) * 18
        x = (k % 14) * 18
        staging[y:y + 18, x:x + 18] = convert(cell.astype(np.uint32), fmt)
    pages = (len(cells) - 1) // 196 + 1
    tex = np.zeros((512, 512), dtype=dtype)
    keys = []
    for p in range(pages):
        tx, ty = (p & 1) * 256, (p >> 1) * 256
        tex[ty:ty + 256, tx:tx + 256] = staging[p * 256:(p + 1) * 256]
        keys.append("%08X" % zlib.crc32(tex.tobytes()))
    return "%08X" % zlib.crc32(staging.tobytes()), keys


if __name__ == "__main__":
    pairs = json.load(open(sys.argv[1]))
    cams = sys.argv[2:] or sorted(pairs)[:6]
    for fmt in ("argb8888", "xrgb8888_noalpha", "argb1555", "argb4444"):
        hits = 0
        for cam in cams:
            obs = pairs[cam][0]
            st, tex = build(cells_for(cam), fmt)
            ok = [st] + tex == obs
            hits += ok
            if fmt == "argb8888" or ok:
                print(f"{fmt:18s} {cam:13s} computed {[st] + tex}  observed {obs}  {'MATCH' if ok else ''}")
        print(f"== {fmt}: {hits}/{len(cams)} cameras match\n")
