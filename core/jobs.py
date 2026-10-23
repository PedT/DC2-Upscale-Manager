"""App context (settings + open workspace) and the background job queue used by the web UI.

Jobs run one at a time on a single worker thread, in submission order:
  extract  - (re)extract all backgrounds from the game folder into the workspace
  run      - execute a prepared upscale run (items already marked 'queued')
Finished jobs produce events (toasts / notifications) the browser picks up by polling.
"""
import collections
import itertools
import json
import os
import threading
import time
import traceback

from .catalog import Catalog
from .extract import extract
from .install import Installer
from .paths import PathError, check_game_dir, check_install_target, check_writable_target, norm
from .runner import Runner, RunnerError, find_upscayl, list_models, models_dir_for, self_check

DEFAULT_CONFIG = {
    "workspace": r"C:\Claude\DinoCrisis2ModWork\upscale_ws",
    "game_dir": r"C:\GOG Games\Dino Crisis 2",
    "mod_dir": r"C:\GOG Games\Dino Crisis 2",    # install target: the game folder itself, or a copy of it
    "upscayl_bin": None,
    "models_dir": None,          # advanced: defaults to the models folder next to upscayl-bin
    "install_16bit": False,      # also install files for the game's 16-bit colour mode (doubles background files)
}


class Context:
    """Settings (config.json next to app.py) plus the catalog/runner of the current workspace."""

    def __init__(self, config_path, log=print):
        self.config_path = config_path
        self.log = log
        self.lock = threading.RLock()
        self.config = dict(DEFAULT_CONFIG)
        if os.path.isfile(config_path):
            with open(config_path, encoding="utf-8") as fh:
                self.config.update(json.load(fh))
        if not self.config.get("upscayl_bin"):
            self.config["upscayl_bin"] = find_upscayl()
        self.cat = None
        self.recovered = []
        self.open_workspace(self.config["workspace"])
        self.save()

    def save(self):
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, indent=1)

    def open_workspace(self, path):
        check_writable_target(path, self.config.get("game_dir"))
        with self.lock:
            if self.cat:
                self.cat.close()
            self.cat = Catalog(path)
            self.config["workspace"] = os.path.abspath(path)
            runner = self.runner()
            self.recovered = runner.recover() if runner else []

    def runner(self):
        b = self.config.get("upscayl_bin")
        if not (b and os.path.isfile(b)):
            return None
        return Runner(self.cat, b, self.config.get("models_dir") or None, log=self.log)

    def installer(self):
        return Installer(self.cat, self.config.get("mod_dir"), self.config.get("game_dir"), log=self.log,
                         include_16bit=bool(self.config.get("install_16bit")))

    def models(self):
        b = self.config.get("upscayl_bin")
        return list_models(self.config.get("models_dir") or models_dir_for(b)) if b else []

    def update(self, changes):
        """Validate and apply settings changes. Returns a list of warnings; raises PathError."""
        warnings = []
        with self.lock:
            new = dict(self.config)
            new.update({k: (v.strip() if isinstance(v, str) else v) for k, v in changes.items()
                        if k in DEFAULT_CONFIG})
            if new.get("game_dir"):
                warnings += check_game_dir(new["game_dir"])
            check_writable_target(new["workspace"], new.get("game_dir"))
            if new.get("mod_dir"):
                check_install_target(new["mod_dir"], new.get("game_dir"))
                if not os.path.isfile(os.path.join(new["mod_dir"], "Dino2.exe")):
                    warnings.append(f"install target has no Dino2.exe yet: {new['mod_dir']}")
            if new.get("upscayl_bin"):
                problems = self_check(new["upscayl_bin"])
                if any("not found" in p or "could not run" in p for p in problems):
                    raise PathError("; ".join(problems))
                warnings += problems
            ws_changed = norm(new["workspace"]) != norm(self.config["workspace"])
            self.config = new
            if ws_changed:
                self.open_workspace(new["workspace"])
            self.save()
        return warnings


class JobManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.queue = collections.deque()
        self.current = None
        self.history = collections.deque(maxlen=30)
        self.events = collections.deque(maxlen=100)
        self._ids = itertools.count(1)
        self._event_ids = itertools.count(1)
        self._cancel = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="jobs").start()

    # -- public --
    def busy(self):
        with self.lock:
            return self.current is not None or bool(self.queue)

    def submit_extract(self):
        return self._submit({"kind": "extract", "title": "Extract images from the game"})

    def submit_install(self):
        return self._submit({"kind": "install", "title": "Install approved images into the game"})

    def submit_uninstall(self):
        return self._submit({"kind": "uninstall", "title": "Uninstall everything this tool installed"})

    def submit_run(self, run_id):
        run = self.ctx.cat.get_run(run_id)
        return self._submit({"kind": "run", "run_id": run_id,
                             "title": f"Run {run_id}: {run['total']} image(s) with {run['model']}"})

    def cancel(self, job_id):
        with self.lock:
            if self.current and self.current["id"] == job_id:
                self._cancel.set()
                return "cancelling"
            for job in list(self.queue):
                if job["id"] == job_id:
                    self.queue.remove(job)
                    break
            else:
                raise KeyError(job_id)
        if job["kind"] == "run":
            self.ctx.runner().cancel_prepared(job["run_id"])
        job.update(state="cancelled", finished=time.time())
        self.history.appendleft(job)
        return "cancelled"

    def snapshot(self, since=0):
        with self.lock:
            return {"current": dict(self.current) if self.current else None,
                    "queue": [dict(j) for j in self.queue],
                    "history": [dict(j) for j in list(self.history)[:10]],
                    "events": [e for e in self.events if e["id"] > since]}

    # -- internals --
    def _submit(self, job):
        job.update(id=next(self._ids), state="queued", created=time.time(), progress=None, error=None)
        with self.lock:
            self.queue.append(job)
        self.wake.set()
        return job

    def _event(self, kind, text, **extra):
        self.events.append({"id": next(self._event_ids), "time": time.time(), "kind": kind, "text": text, **extra})

    def _loop(self):
        while True:
            self.wake.wait(1.0)
            with self.lock:
                if not self.queue:
                    self.wake.clear()
                    continue
                job = self.queue.popleft()
                job.update(state="running", started=time.time())
                self.current = job
                self._cancel.clear()
            try:
                {"extract": self._do_extract, "run": self._do_run, "install": self._do_install,
                 "uninstall": self._do_uninstall}[job["kind"]](job)
            except Exception as exc:                      # report, never kill the worker thread
                job.update(state="failed", error=f"{exc}")
                self.ctx.log(traceback.format_exc())
                self._event("error", f"{job['title']} failed: {exc}")
            job["finished"] = time.time()
            with self.lock:
                self.current = None
                self.history.appendleft(job)

    def _do_extract(self, job):
        def progress(done, total, msg):
            job["progress"] = {"done": done, "total": total, "message": msg}

        cfg = self.ctx.config
        summary = extract(cfg["game_dir"], cfg["workspace"], progress=progress, log=self.ctx.log)
        job.update(state="done", result=summary)
        self._event("extract_done", f"Extraction finished: {summary['fullscreen']} menu images, "
                                    f"{summary['rooms']} room views ({summary['new']} new, "
                                    f"{summary['changed']} changed)")

    def _do_install(self, job):
        def progress(done, total, msg):
            job["progress"] = {"done": done, "total": total, "message": msg}

        s = self.ctx.installer().install(progress=progress)
        job.update(state="done", result=s)
        where = "the game folder" if s["target_is_game"] else s["target"]
        text = (f"Install finished: {s['items']} image(s) installed in {where}, {s['masks']} with HD masks "
                f"({s['added']} files added, {s['updated']} updated, {s['unchanged']} unchanged, {s['removed']} removed)")
        if s["skipped_existing"]:
            text += (f"; {len(s['skipped_existing'])} file(s) with the same name already existed and were left "
                     f"untouched (not installed by this tool)")
        if s["overlay_rooms"]:
            text += f"; {len(s['overlay_rooms'])} room(s) keep their original moving overlay sprite (boat, fence)"
        if s["fallback_tiles"]:
            text += f"; {len(s['fallback_tiles'])} room(s) used enlarged original tiles for a few mask pieces"
        if s["blocked"]:
            text += f"; {len(s['blocked'])} approved image(s) not installable yet"
        self._event("install_done", text, **{k: s[k] for k in ("added", "updated", "removed", "items")})

    def _do_uninstall(self, job):
        s = self.ctx.installer().uninstall()
        job.update(state="done", result=s)
        text = f"Uninstall finished: {s['removed']} file(s) removed"
        if s["kept_modified"]:
            text += f"; {len(s['kept_modified'])} file(s) were changed outside the tool and left in place"
        self._event("uninstall_done", text)

    def _do_run(self, job):
        runner = self.ctx.runner()
        if runner is None:
            raise RunnerError("upscayl-bin is not configured")

        def progress(p):
            job["progress"] = p

        run = runner.execute(job["run_id"], progress=progress, cancel=self._cancel)
        job.update(state=run["state"], result={"done": run["done"], "failed": run["failed"]})
        verb = {"done": "finished", "cancelled": "was cancelled", "failed": "failed"}.get(run["state"], run["state"])
        self._event("run_done", f"Run {run['id']} {verb}: {run['done']} upscaled, {run['failed']} failed",
                    run_id=run["id"], done=run["done"], failed=run["failed"], state=run["state"])
