"""Upscale catalog items with Upscayl's headless engine (milestone M2, command line).

Usage (workspace defaults to ..\\upscale_ws):
  python run_cli.py check                                  find upscayl-bin, verify flags, list models
  python run_cli.py run --model ultrasharp-4x --items "TITLE.DAT#5" "ST100.DBS#0"
  python run_cli.py run --model remacri-4x --kind room --stage ST100 [--limit 10]
  python run_cli.py run --model ultrasharp-4x --kind fullscreen
  python run_cli.py runs                                   list runs
  python run_cli.py resume <run_id>                        re-run the leftovers of an interrupted run

Only items in 'todo' are taken; duplicates and blank placeholders are skipped automatically.
Results land in runs\\<id>\\out and the items move to 'review'. Ctrl+C cancels a run cleanly
(finished images are kept, the rest go back to 'todo').
Options for run/resume: --scale 2|3|4 (default 4), --gpu 0, --threads 1:2:2, --workers N, --bin PATH
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.catalog import Catalog  # noqa: E402
from core.runner import (Runner, RunnerError, find_upscayl, list_models,  # noqa: E402
                         models_dir_for, self_check)

DEFAULT_WS = r"C:\Claude\DinoCrisis2ModWork\upscale_ws"


def fmt_s(s):
    if s is None:
        return "--:--"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def open_runner(a):
    cat = Catalog(a.workspace)
    bin_path = find_upscayl(a.bin or cat.get_setting("upscayl_bin"))
    if not bin_path:
        sys.exit("error: upscayl-bin.exe not found; pass --bin <path to upscayl-bin.exe>")
    cat.set_setting("upscayl_bin", bin_path)
    runner = Runner(cat, bin_path, log=lambda m: print("  " + m))
    for run_id in runner.recover():
        left = runner.leftovers(run_id)
        print(f"note: run {run_id} was interrupted; finished images kept, {len(left)} left over "
              f"(continue with: run_cli.py resume {run_id})")
    return cat, runner


def execute(runner, run_id):
    cancel = threading.Event()

    def progress(p):
        print(f"\r  run {p['run_id']}: {p['done']}/{p['total']} done, {p['failed']} failed, "
              f"current {p['current_pct']:5.1f}%, elapsed {fmt_s(p['elapsed_s'])}, eta {fmt_s(p['eta_s'])}   ",
              end="", flush=True)

    result = {}
    worker = threading.Thread(target=lambda: result.update(run=runner.execute(run_id, progress, cancel)))
    worker.start()
    try:
        while worker.is_alive():
            worker.join(timeout=0.3)
    except KeyboardInterrupt:
        print("\n  cancelling...")
        cancel.set()
        worker.join()
    print()
    run = result.get("run") or runner.cat.get_run(run_id)
    print(f"run {run_id} {run['state']}: {run['done']} upscaled, {run['failed']} failed "
          f"-> {os.path.normpath(os.path.join(runner.ws, run['params']['dir'], 'out'))}")
    if run["failed"]:
        print("  last output lines:\n    " + "\n    ".join((run["log"] or "").splitlines()[-8:]))


def select_items(cat, a):
    if a.items:
        return a.items
    items = cat.items(kind=a.kind, status="todo", include_dups=False, include_placeholders=False)
    if a.stage:
        items = [i for i in items if (i["stage"] or "").upper() == a.stage.upper()]
    if a.limit:
        items = items[:a.limit]
    return [i["id"] for i in items]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=DEFAULT_WS)
    ap.add_argument("--bin", help="path to upscayl-bin.exe (auto-detected and remembered)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("runs")
    r = sub.add_parser("run")
    r.add_argument("--model", required=True)
    r.add_argument("--items", nargs="+", help="item ids, e.g. TITLE.DAT#5 ST100.DBS#0")
    r.add_argument("--kind", choices=("fullscreen", "room"))
    r.add_argument("--stage", help="room stage, e.g. ST100")
    r.add_argument("--limit", type=int)
    for p in (r, sub.add_parser("resume")):
        p.add_argument("--scale", type=int, default=4)
        p.add_argument("--gpu")
        p.add_argument("--threads")
        p.add_argument("--workers", type=int, default=1)
    sub.choices["resume"].add_argument("run_id", type=int)
    a = ap.parse_args()

    if a.cmd == "check":
        cat = Catalog(a.workspace)
        bin_path = find_upscayl(a.bin or cat.get_setting("upscayl_bin"))
        print("upscayl-bin:", bin_path or "NOT FOUND")
        if bin_path:
            problems = self_check(bin_path)
            print("self-check:", "OK" if not problems else "; ".join(problems))
            print("models:", ", ".join(list_models(models_dir_for(bin_path))))
            cat.set_setting("upscayl_bin", bin_path)
        return

    cat, runner = open_runner(a)
    if a.cmd == "runs":
        for run in cat.runs():
            took = f"{fmt_s(run['finished'] - run['started'])}" if run["finished"] and run["started"] else ""
            print(f"  {run['id']:4d}  {run['state']:11s} {run['model']:22s} x{run['scale']}  "
                  f"{run['done']}/{run['total']} done, {run['failed']} failed  {took}")
        return
    try:
        if a.cmd == "resume":
            run_id, skipped = runner.resume(a.run_id)
        else:
            ids = select_items(cat, a)
            if not ids:
                sys.exit("nothing selected (only 'todo' items that aren't duplicates/placeholders are taken)")
            run_id, skipped = runner.prepare(ids, a.model, a.scale, a.gpu, a.threads, a.workers)
    except RunnerError as exc:
        sys.exit(f"error: {exc}")
    for item_id, reason in skipped:
        print(f"  skipped {item_id}: {reason}")
    print(f"run {run_id}: {cat.get_run(run_id)['total']} image(s) with {cat.get_run(run_id)['model']}")
    t0 = time.time()
    execute(runner, run_id)
    print(f"  took {fmt_s(time.time() - t0)}")


if __name__ == "__main__":
    main()
