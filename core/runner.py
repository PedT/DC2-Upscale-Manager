"""Upscayl job runner: feeds catalog items through upscayl-bin.exe (headless) and records candidates.

How a run works:
  prepare()  - picks eligible items (status todo, not a duplicate, not a blank placeholder), links their
               originals into runs/<id>/in/w<k>/ (one folder per worker process), marks them 'queued'.
  execute()  - starts one upscayl-bin process per worker folder with no console window, parses its
               output ("... -> <file> done" per image, "NN,NN%" progress), validates each result,
               registers it as a candidate and moves the item to 'review'. Supports cancel.
  recover()  - after a crash or closed app: runs left 'running'/'queued' by a dead process are marked
               'interrupted', finished outputs are kept, leftover items go back to 'todo'.
  resume()   - starts a new run (same settings) for an interrupted/cancelled run's leftovers.

Upscaling is GPU-bound: one process fed a whole folder is the efficient default (workers=1).
"""
import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from PIL import Image

CREATE_NO_WINDOW = 0x08000000
REQUIRED_FLAGS = ("-i", "-o", "-m", "-n", "-s", "-f", "-g", "-j")
DONE_RE = re.compile(r"->\s*(.+?)\s+done\s*$")
PCT_RE = re.compile(r"(\d+)[.,](\d+)\s*%")
LOG_TAIL = 200


class RunnerError(RuntimeError):
    pass


# ---- locating Upscayl ---------------------------------------------------------------------------
def default_bin_candidates():
    paths = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Upscayl")]
    if os.environ.get("LOCALAPPDATA"):
        paths.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "Upscayl"))
    return [os.path.join(p, "resources", "bin", "upscayl-bin.exe") for p in paths]


def find_upscayl(preferred=None):
    for path in ([preferred] if preferred else []) + default_bin_candidates():
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def models_dir_for(bin_path):
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(bin_path))), "models")


def list_models(models_dir):
    if not os.path.isdir(models_dir):
        return []
    names = {os.path.splitext(n)[0] for n in os.listdir(models_dir) if n.endswith(".param")}
    return sorted(n for n in names if os.path.isfile(os.path.join(models_dir, n + ".bin")))


def _command(bin_path):
    # A .py "binary" is only used by the tests (fake upscayl).
    return [sys.executable, "-I", bin_path] if bin_path.lower().endswith(".py") else [bin_path]


def _popen_flags():
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def self_check(bin_path):
    """Run '<bin> -h' and confirm every flag we use is still documented. Returns a list of problems."""
    if not bin_path or not os.path.isfile(bin_path):
        return [f"upscayl-bin not found: {bin_path}"]
    try:
        res = subprocess.run(_command(bin_path) + ["-h"], capture_output=True, timeout=60,
                             creationflags=_popen_flags())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"could not run {bin_path}: {exc}"]
    text = (res.stdout + res.stderr).decode("utf-8", "replace")
    problems = [f"flag {f} missing from '{os.path.basename(bin_path)} -h' output"
                for f in REQUIRED_FLAGS if not re.search(rf"(?m)^\s*{re.escape(f)}\s", text)]
    models = models_dir_for(bin_path)
    if not list_models(models):
        problems.append(f"no models found in {models}")
    return problems


def pid_alive(pid):
    """True if a process with this id is running (Windows-safe; os.kill(pid, 0) is not)."""
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
    k32.CloseHandle(handle)
    return bool(ok) and code.value == 259                      # STILL_ACTIVE


def _link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


# ---- the runner ---------------------------------------------------------------------------------
class Runner:
    def __init__(self, catalog, bin_path, models_dir=None, log=print):
        self.cat = catalog
        self.ws = catalog.workspace
        self.bin = bin_path
        self.models_dir = models_dir or models_dir_for(bin_path)
        self.log = log

    # -- selection --
    def eligible(self, item_ids):
        ok, skipped = [], []
        for item_id in item_ids:
            it = self.cat.get_item(item_id)
            if it is None:
                skipped.append((item_id, "unknown item"))
            elif it["dup_of"]:
                skipped.append((item_id, f"duplicate of {it['dup_of']}"))
            elif it["placeholder"]:
                skipped.append((item_id, "blank placeholder"))
            elif it["status"] != "todo":
                skipped.append((item_id, f"status is {it['status']}"))
            else:
                ok.append(it)
        return ok, skipped

    def prepare(self, item_ids, model, scale=4, gpu=None, threads=None, workers=1):
        """Create a run for the eligible items. Returns (run_id, skipped)."""
        if model not in list_models(self.models_dir):
            raise RunnerError(f"model {model!r} not found in {self.models_dir}")
        if scale not in (2, 3, 4):
            raise RunnerError("scale must be 2, 3 or 4")
        items, skipped = self.eligible(item_ids)
        if not items:
            raise RunnerError("nothing to upscale: " + "; ".join(f"{i}: {r}" for i, r in skipped[:5]))
        workers = max(1, min(int(workers), len(items)))
        params = {"gpu": gpu, "threads": threads, "workers": workers, "pid": os.getpid(),
                  "skipped": len(skipped)}
        run_id = self.cat.create_run(model, scale, params, len(items))
        rel_dir = f"runs/{run_id:05d}"
        params["dir"] = rel_dir
        self.cat.update_run(run_id, params=params)
        run_dir = os.path.join(self.ws, rel_dir)
        mapping = {}
        for k, it in enumerate(items):
            wdir = os.path.join(run_dir, "in", f"w{k % workers}")
            os.makedirs(wdir, exist_ok=True)
            fname = os.path.basename(it["original_png"])
            _link_or_copy(os.path.join(self.ws, it["original_png"]), os.path.join(wdir, fname))
            mapping[fname] = it["id"]
            self.cat.set_status(it["id"], "queued")
        os.makedirs(os.path.join(run_dir, "out"), exist_ok=True)
        with open(os.path.join(run_dir, "items.json"), "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, indent=1)
        return run_id, skipped

    # -- execution --
    def execute(self, run_id, progress=None, cancel=None):
        """Run upscayl over a prepared run. Blocks until finished or cancelled. Returns the run row."""
        run = self.cat.get_run(run_id)
        if run is None or run["state"] != "queued":
            raise RunnerError(f"run {run_id} is not queued")
        params = run["params"]
        params["pid"] = os.getpid()
        run_dir = os.path.join(self.ws, params["dir"])
        out_dir = os.path.join(run_dir, "out")
        with open(os.path.join(run_dir, "items.json"), encoding="utf-8") as fh:
            mapping = json.load(fh)
        pending = {}
        for fname, item_id in mapping.items():
            it = self.cat.get_item(item_id)
            if it and it["status"] == "queued":
                self.cat.set_status(item_id, "upscaling")
                pending[fname] = item_id
        started = time.time()
        self.cat.update_run(run_id, state="running", started=started, params=params)

        events, tail, pct = queue.Queue(), [], {"value": 0.0}
        procs = []
        for wname in sorted(os.listdir(os.path.join(run_dir, "in"))):
            wdir = os.path.join(run_dir, "in", wname)
            if not any(f in pending for f in os.listdir(wdir)):
                continue
            cmd = _command(self.bin) + ["-i", wdir, "-o", out_dir, "-m", self.models_dir, "-n", run["model"],
                                        "-s", str(run["scale"]), "-f", "png"]
            if params.get("gpu") not in (None, ""):
                cmd += ["-g", str(params["gpu"])]
            if params.get("threads"):
                cmd += ["-j", str(params["threads"])]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=_popen_flags())
            reader = threading.Thread(target=self._read_output, args=(proc, events, tail, pct), daemon=True)
            reader.start()
            procs.append((proc, reader))

        stats = {"done": 0, "failed": 0, "times": []}
        last_report = 0.0
        cancelled = False
        sizes = {}                                  # output file -> size seen at the previous check
        while True:
            alive = any(p.poll() is None for p, _ in procs)
            # Finished images: upscayl's "-> file done" lines only appear in verbose mode, so the
            # output folder itself is the main signal. A file counts once its size is stable
            # between two checks (fully written) and it opens at the expected size.
            ready = set()
            while not events.empty():
                ready.add(events.get())
            for fname in list(pending):
                try:
                    size = os.path.getsize(os.path.join(out_dir, fname))
                except OSError:
                    continue
                if size and sizes.get(fname) == size:
                    ready.add(fname)
                sizes[fname] = size
            for fname in ready:
                if fname in pending and self._collect(run, fname, pending[fname], out_dir):
                    del pending[fname]
                    stats["done"] += 1
                    stats["times"].append(time.time())
            if cancel is not None and cancel.is_set():
                cancelled = True
                for p, _ in procs:
                    if p.poll() is None:
                        p.kill()
                break
            now = time.time()
            if progress and (now - last_report >= 0.5 or not alive):
                last_report = now
                progress(self._progress(run, stats, pct["value"], started))
            if not alive and events.empty():
                break
            time.sleep(0.2)
        for p, reader in procs:
            p.wait()
            reader.join(timeout=5)
            p.stderr.close()

        # Final sweep: outputs whose 'done' line was missed (or that arrived during cancel).
        for fname in list(pending):
            if self._collect(run, fname, pending[fname], out_dir):
                del pending[fname]
                stats["done"] += 1
        for fname, item_id in pending.items():
            self.cat.set_status(item_id, "todo")
            if not cancelled:
                self.cat.add_note(item_id, f"upscale failed in run {run_id} ({run['model']})")
                stats["failed"] += 1
        codes = [p.returncode for p, _ in procs]
        state = "cancelled" if cancelled else ("failed" if stats["done"] == 0 else "done")
        self.cat.update_run(run_id, state=state, finished=time.time(), done=stats["done"],
                            failed=stats["failed"], log="\n".join([f"exit codes: {codes}"] + tail[-LOG_TAIL:]))
        if progress:
            progress(self._progress(self.cat.get_run(run_id), stats, 100.0, started))
        return self.cat.get_run(run_id)

    @staticmethod
    def _read_output(proc, events, tail, pct):
        for raw in proc.stderr:
            for part in re.split(rb"[\r\n]", raw):
                line = part.decode("utf-8", "replace").strip()
                if not line:
                    continue
                m = DONE_RE.search(line)
                if m:
                    events.put(os.path.basename(m.group(1).strip()))
                else:
                    m = PCT_RE.search(line)
                    if m:
                        pct["value"] = float(f"{m.group(1)}.{m.group(2)}")
                        continue
                tail.append(line)
                del tail[:-LOG_TAIL]

    def _collect(self, run, fname, item_id, out_dir):
        """Validate one output and register it. False if missing/incomplete (retried in the final sweep)."""
        out = os.path.join(out_dir, fname)
        src = os.path.join(self.ws, self.cat.get_item(item_id)["original_png"])
        try:
            with Image.open(src) as im:
                want = (im.width * run["scale"], im.height * run["scale"])
            with Image.open(out) as im:
                im.load()
                if im.size != want:
                    self.log(f"{fname}: output is {im.size}, expected {want}")
                    return False
        except (OSError, SyntaxError):
            return False
        rel = os.path.relpath(out, self.ws).replace("\\", "/")
        self.cat.add_candidate(item_id, run["id"], run["model"], rel)
        self.cat.set_status(item_id, "review")
        return True

    @staticmethod
    def _progress(run, stats, pct, started):
        done, total = stats["done"], run["total"]
        times = stats["times"]
        eta = None
        if times and done < total:
            # Average over the whole run so far: upscayl often finishes images in pairs,
            # so the gap between the last two completions says little.
            per_image = (times[-1] - started) / len(times)
            eta = per_image * (total - done - stats["failed"])
        return {"run_id": run["id"], "model": run["model"], "total": total, "done": done,
                "failed": stats["failed"], "current_pct": pct, "elapsed_s": time.time() - started,
                "eta_s": eta}

    # -- crash recovery --
    def recover(self):
        """Mark runs abandoned by a dead process as interrupted; keep finished outputs. Returns run ids."""
        recovered = []
        for run in self.cat.runs():
            if run["state"] not in ("queued", "running"):
                continue
            pid = run["params"].get("pid")
            if pid == os.getpid() or pid_alive(pid):
                continue
            run_dir = os.path.join(self.ws, run["params"].get("dir", ""))
            mapping = {}
            items_json = os.path.join(run_dir, "items.json")
            if os.path.isfile(items_json):
                with open(items_json, encoding="utf-8") as fh:
                    mapping = json.load(fh)
            done = 0
            for fname, item_id in mapping.items():
                it = self.cat.get_item(item_id)
                if not it or it["status"] not in ("queued", "upscaling"):
                    continue
                if it["status"] == "upscaling" and self._collect(run, fname, item_id, os.path.join(run_dir, "out")):
                    done += 1
                    continue
                self.cat.set_status(item_id, "todo")
            self.cat.update_run(run["id"], state="interrupted", finished=time.time(),
                                done=(run["done"] or 0) + done)
            recovered.append(run["id"])
        return recovered

    def cancel_prepared(self, run_id):
        """Cancel a run that was prepared but never started: its items go back to 'todo'."""
        run = self.cat.get_run(run_id)
        if run is None or run["state"] != "queued":
            raise RunnerError(f"run {run_id} is not waiting in the queue")
        with open(os.path.join(self.ws, run["params"]["dir"], "items.json"), encoding="utf-8") as fh:
            for item_id in json.load(fh).values():
                if self.cat.get_item(item_id)["status"] == "queued":
                    self.cat.set_status(item_id, "todo")
        self.cat.update_run(run_id, state="cancelled", finished=time.time())

    def leftovers(self, run_id):
        """Items of a finished/interrupted run that were not upscaled and are back in 'todo'."""
        run = self.cat.get_run(run_id)
        run_dir = os.path.join(self.ws, run["params"].get("dir", ""))
        with open(os.path.join(run_dir, "items.json"), encoding="utf-8") as fh:
            mapping = json.load(fh)
        got = {c["item_id"] for c in self.cat.candidates(run_id=run_id)}
        return [i for i in mapping.values() if i not in got and self.cat.get_item(i)["status"] == "todo"]

    def resume(self, run_id):
        """Prepare a new run (same model/settings) for an earlier run's leftovers. Returns (run_id, skipped)."""
        run = self.cat.get_run(run_id)
        if run["state"] not in ("interrupted", "cancelled", "failed", "done"):
            raise RunnerError(f"run {run_id} is {run['state']}")
        items = self.leftovers(run_id)
        if not items:
            raise RunnerError(f"run {run_id} has no leftover items")
        p = run["params"]
        return self.prepare(items, run["model"], run["scale"], p.get("gpu"), p.get("threads"), p.get("workers", 1))
