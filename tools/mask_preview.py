"""Preview a room's HD mask before installing it.

Usage:  python tools/mask_preview.py "ST101.DBS#0" [--out C:\\some\\folder]
Uses the item's approved image (or a plain Lanczos 4x of the original if it isn't approved yet) and writes:
  <id>_mask_check.png   the HD background darkened, with every mask piece drawn back at full brightness
                        (they should line up with the objects characters walk behind)
  <id>_mask_atlas.png   the 2048x2048 texture that Install would encrypt
"""
import argparse
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image  # noqa: E402

from core.catalog import Catalog  # noqa: E402
from core.masks import MaskSource, hd_atlas, preview  # noqa: E402

WS = r"C:\Claude\DinoCrisis2ModWork\upscale_ws"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("item")
    ap.add_argument("--workspace", default=WS)
    ap.add_argument("--out", default=os.path.join(WS, "mask_previews"))
    a = ap.parse_args()
    cat = Catalog(a.workspace)
    it = cat.get_item(a.item)
    if not it or it["kind"] != "room":
        sys.exit(f"{a.item} is not a room item")
    cam = MaskSource(cat.get_setting("game_dir") or r"C:\GOG Games\Dino Crisis 2").camera(it["source_file"], it["grp"])
    if cam.kind != "cells":
        sys.exit(f"{a.item} has no mask tiles ({cam.kind})")
    master = cat.get_item(it["dup_of"]) if it["dup_of"] else it
    if master["approved_png"]:
        hd, source = Image.open(os.path.join(a.workspace, master["approved_png"])), "approved image"
    else:
        hd, source = Image.open(io.BytesIO(cam.jpeg)).resize((1280, 960), Image.LANCZOS), "Lanczos 4x of the original"
    atlas, stats = hd_atlas(cam, hd)
    os.makedirs(a.out, exist_ok=True)
    base = os.path.join(a.out, a.item.replace("#", "_").replace(".", "_"))
    preview(cam, hd, atlas).save(base + "_mask_check.png")
    atlas.save(base + "_mask_atlas.png")
    print(f"{a.item}: {stats['cells']} mask pieces on {stats['pages']} page(s), key {cam.key}, from the {source}"
          f"{'; ' + str(stats['fallback_cells']) + ' piece(s) used the enlarged original' if stats['fallback_cells'] else ''}")
    print(f"wrote {base}_mask_check.png and {base}_mask_atlas.png")
    cat.close()


if __name__ == "__main__":
    main()
