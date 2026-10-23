"""Extract all Dino Crisis 2 backgrounds into an upscaler workspace (milestone M1, command line).

Usage:
  python extract_cli.py --game "C:\\GOG Games\\Dino Crisis 2" --workspace "C:\\Claude\\DinoCrisis2ModWork\\upscale_ws"
  python extract_cli.py --workspace ... --status        (show catalog counts only)

The game folder is only read. Re-running is safe: unchanged images are skipped and the
approve/reject state of every item is kept.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.catalog import Catalog  # noqa: E402
from core.extract import extract  # noqa: E402
from core.paths import PathError  # noqa: E402

DEFAULT_WS = r"C:\Claude\DinoCrisis2ModWork\upscale_ws"


def show_status(workspace):
    cat = Catalog(workspace)
    for kind, by_status in sorted(cat.counts().items()):
        total = sum(by_status.values())
        parts = ", ".join(f"{s} {n}" for s, n in sorted(by_status.items()))
        print(f"{kind:10s} {total:5d}  ({parts})")
    last = cat.get_setting("last_extract")
    if last:
        print(f"last extraction: {last['fullscreen']} full-screen, {last['rooms']} room views, "
              f"{last['duplicates']} duplicates, {last['placeholders']} placeholders, "
              f"{last['no_key']} without key")
    cat.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", help="Dino Crisis 2 install folder (read-only)")
    ap.add_argument("--workspace", default=DEFAULT_WS, help=f"output workspace (default {DEFAULT_WS})")
    ap.add_argument("--status", action="store_true", help="print catalog counts and exit")
    a = ap.parse_args()
    if a.status:
        show_status(a.workspace)
        return
    if not a.game:
        ap.error("--game is required")

    t0 = time.time()
    last = [0.0]

    def progress(done, total, msg):
        now = time.time()
        if total and (now - last[0] > 0.5 or done == total):
            last[0] = now
            print(f"\r  {done}/{total}  {msg:<24}", end="", flush=True)
        elif not total:
            print(f"  {msg}...")

    try:
        s = extract(a.game, a.workspace, progress=progress, log=lambda m: print("  " + m))
    except PathError as exc:
        sys.exit(f"error: {exc}")
    print()
    print(f"done in {time.time() - t0:.1f}s -> {os.path.abspath(a.workspace)}")
    print(f"  full-screen images: {s['fullscreen']}   room views: {s['rooms']}")
    print(f"  duplicates: {s['duplicates']}   blank placeholders: {s['placeholders']}   rooms without key: {s['no_key']}")
    print(f"  new: {s['new']}   changed: {s['changed']}   unchanged: {s['unchanged']}")
    for w in s["warnings"]:
        print("  WARNING:", w)


if __name__ == "__main__":
    main()
