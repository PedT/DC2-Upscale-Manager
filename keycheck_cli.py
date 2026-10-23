"""Confirm replacement keys from a Process Monitor CSV log (see ..\\KEY_CHECK_PROCEDURE.md).

Usage:
  python keycheck_cli.py C:\\path\\dc2_keycheck.csv            analyse only (changes nothing)
  python keycheck_cli.py C:\\path\\dc2_keycheck.csv --apply    analyse and record the result in the catalog
Options: --workspace (default ..\\upscale_ws), --game (default: the game folder saved by the extraction)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import keycheck  # noqa: E402
from core.catalog import Catalog  # noqa: E402

DEFAULT_WS = r"C:\Claude\DinoCrisis2ModWork\upscale_ws"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workspace", default=DEFAULT_WS)
    ap.add_argument("--game")
    a = ap.parse_args()
    cat = Catalog(a.workspace)
    game = a.game or cat.get_setting("game_dir") or r"C:\GOG Games\Dino Crisis 2"
    try:
        with open(a.log, "rb") as fh:
            report = keycheck.analyze(cat, keycheck.read_procmon_csv(fh.read()), game)
    except (OSError, keycheck.KeyCheckError) as exc:
        sys.exit(f"error: {exc}")
    c = report["room_counts"]
    print(f"{report['events']} events, {report['probes']} replacement lookups ({report['unique_probes']} different keys)")
    print(f"menus seen: {', '.join(report['fullscreen_verified']) or 'none'}")
    print(f"rooms seen: {c['primary']} primary, {c['alt']} alternative, {c['both']} where both predictions agree")
    for item_id, probes in list(report["contradicted"].items())[:20]:
        print(f"  no matching key for {item_id}; unknown keys looked up after it: {', '.join(probes) or '-'}")
    print(f"\n{report['decision'].upper()}: {report['message']}")
    if a.apply:
        r = keycheck.apply(cat, report)
        print(f"applied: {r['changed']} item(s) updated, {r['verified']} seen in game, room variant: {r['variant']}")
    else:
        print("(nothing changed; add --apply to record this result)")
    cat.close()


if __name__ == "__main__":
    main()
