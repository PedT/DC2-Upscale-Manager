"""DC2 Upscale Manager - local web UI (milestone M3: setup, library, runs).

Start:  python -I app.py [--port 8765] [--no-browser]
Then use the page that opens at http://127.0.0.1:<port>. The server only listens on this PC.
"""
import argparse
import os
import socket
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from flask import Flask, abort, jsonify, request, send_from_directory  # noqa: E402
except ImportError:
    sys.exit("Flask is not installed. Install it with:  python -m pip install flask")

from core.jobs import Context, JobManager  # noqa: E402
from core.maintenance import cleanup, disk_usage  # noqa: E402
from core import keycheck  # noqa: E402
from core.paths import PathError  # noqa: E402
from core.review import Review, ReviewError  # noqa: E402
from core.runner import RunnerError, self_check  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ITEM_FIELDS = ("id", "kind", "label", "stage", "camera", "status", "models_tried", "key_status", "key32",
               "dup_of", "placeholder", "thumb", "original_png", "approved_png", "notes", "installed")
_browse_lock = threading.Lock()


def create_app(config_path=None, log=print):
    app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
    ctx = Context(config_path or os.path.join(HERE, "config.json"), log=log)
    jobs = JobManager(ctx)
    app.config.update(CTX=ctx, JOBS=jobs)

    def err(msg, code=400):
        return jsonify({"error": msg}), code

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/ws/<path:rel>")
    def workspace_file(rel):
        # Only images inside the workspace; send_from_directory rejects ../ traversal.
        if not rel.lower().endswith((".png", ".jpg")):
            abort(404)
        return send_from_directory(ctx.config["workspace"], rel, max_age=3600)

    @app.get("/api/state")
    def state():
        since = request.args.get("since", 0, type=int)
        cat = ctx.cat
        b = ctx.config.get("upscayl_bin")
        return jsonify({
            "config": ctx.config,
            "models": ctx.models(),
            "upscayl_ok": bool(b and os.path.isfile(b)),
            "version": cat.version(),
            "counts": cat.counts(),
            "last_extract": cat.get_setting("last_extract"),
            "exe_warnings": cat.get_setting("exe_warnings", []),
            "recovered_runs": ctx.recovered,
            "jobs": jobs.snapshot(since),
        })

    @app.get("/api/items")
    def items():
        return jsonify([{k: it[k] for k in ITEM_FIELDS} for it in ctx.cat.items()])

    @app.post("/api/config")
    def config():
        if jobs.busy():
            return err("wait until the running job finishes before changing settings")
        try:
            warnings = ctx.update(request.get_json(force=True) or {})
        except (PathError, ValueError) as exc:
            return err(str(exc))
        return jsonify({"ok": True, "warnings": warnings, "config": ctx.config})

    @app.post("/api/selfcheck")
    def selfcheck():
        b = ctx.config.get("upscayl_bin")
        return jsonify({"problems": self_check(b), "models": ctx.models()})

    @app.post("/api/browse")
    def browse():
        body = request.get_json(force=True) or {}
        path = browse_dialog(body.get("kind", "folder"), body.get("initial"), body.get("title", "Select"))
        return jsonify({"path": path})

    @app.post("/api/extract")
    def start_extract():
        if not ctx.config.get("game_dir"):
            return err("set the game folder first")
        return jsonify(jobs.submit_extract())

    @app.post("/api/run")
    def start_run():
        body = request.get_json(force=True) or {}
        runner = ctx.runner()
        if runner is None:
            return err("upscayl-bin is not configured (Setup tab)")
        try:
            run_id, skipped = runner.prepare(body.get("item_ids") or [], body.get("model", ""),
                                             int(body.get("scale", 4)), body.get("gpu") or None,
                                             body.get("threads") or None, int(body.get("workers", 1)))
        except (RunnerError, ValueError) as exc:
            return err(str(exc))
        job = jobs.submit_run(run_id)
        return jsonify({"run_id": run_id, "job": job, "skipped": skipped})

    @app.post("/api/jobs/<int:job_id>/cancel")
    def cancel_job(job_id):
        try:
            return jsonify({"result": jobs.cancel(job_id)})
        except (KeyError, RunnerError) as exc:
            return err(f"cannot cancel job {job_id}: {exc}")

    @app.get("/api/runs")
    def runs():
        runner = ctx.runner()
        out = []
        for r in reversed(ctx.cat.runs()):
            r = dict(r)
            r["leftovers"] = 0
            if runner and r["state"] in ("interrupted", "cancelled", "failed", "done"):
                try:
                    r["leftovers"] = len(runner.leftovers(r["id"]))
                except OSError:
                    pass
            r["log_tail"] = "\n".join((r.pop("log") or "").splitlines()[-15:])
            out.append(r)
        return jsonify(out)

    @app.post("/api/runs/<int:run_id>/resume")
    def resume(run_id):
        runner = ctx.runner()
        try:
            new_id, skipped = runner.resume(run_id)
        except (RunnerError, AttributeError) as exc:
            return err(str(exc))
        return jsonify({"run_id": new_id, "job": jobs.submit_run(new_id), "skipped": skipped})

    # ---- review (M4) ----------------------------------------------------------------------
    def review():
        return Review(ctx.cat)

    @app.get("/api/review")
    def review_queue():
        out = []
        for entry in review().queue():
            it, c = entry["item"], entry["candidate"]
            out.append({**{k: it[k] for k in ITEM_FIELDS},
                        "candidate": {k: c[k] for k in ("id", "path", "model", "run_id", "created")}})
        return jsonify(out)

    @app.get("/cthumb/<path:rel>")
    def candidate_thumb(rel):
        if not rel.startswith("runs/") or not rel.lower().endswith(".png") or ".." in rel.split("/"):
            abort(404)
        try:
            thumb = review().candidate_thumb(rel)
        except (OSError, FileNotFoundError):
            abort(404)
        return send_from_directory(ctx.config["workspace"], thumb, max_age=600)

    @app.post("/api/review/<action>")
    def review_action(action):
        if action not in ("approve", "reject", "reopen"):
            abort(404)
        body = request.get_json(force=True) or {}
        ids = body.get("item_ids") or []
        kw = {"delete_file": bool(body.get("delete_file", True))} if action == "reject" else {}
        return jsonify(review().batch(action, ids, **kw))

    @app.post("/api/items/replace")
    def replace_approved():
        item_id = request.args.get("id", "")
        f = request.files.get("file")
        if not f:
            return err("no file uploaded")
        try:
            rel = review().replace_approved(item_id, f.read(), f.filename or "edited.png")
        except ReviewError as exc:
            return err(str(exc))
        return jsonify({"ok": True, "approved_png": rel})

    # ---- install (M5) ---------------------------------------------------------------------
    @app.get("/api/install")
    def install_status():
        inst = ctx.installer()
        out = inst.status()
        try:
            out["warnings"], out["error"] = inst.check_target(), None
        except PathError as exc:
            out["warnings"], out["error"] = [], str(exc)
        return jsonify(out)

    @app.post("/api/install")
    def install():
        try:
            ctx.installer().check_target()
        except PathError as exc:
            return err(str(exc))
        return jsonify(jobs.submit_install())

    @app.post("/api/uninstall")
    def uninstall():
        return jsonify(jobs.submit_uninstall())

    # ---- in-game key check (phase 2, step 1) ----------------------------------------------
    last_report = {}

    def key_counts():
        out = {}
        for it in ctx.cat.items(include_dups=False, include_placeholders=False):
            out.setdefault(it["kind"], {}).setdefault(it["key_status"], 0)
            out[it["kind"]][it["key_status"]] += 1
        return out

    @app.get("/api/keycheck")
    def keycheck_state():
        return jsonify({"state": ctx.cat.get_setting(keycheck.SETTING, {}) or {}, "counts": key_counts(),
                        "last_report": last_report.get("report")})

    @app.post("/api/keycheck/analyze")
    def keycheck_analyze():
        f = request.files.get("file")
        if not f:
            return err("no file uploaded")
        try:
            events = keycheck.read_procmon_csv(f.read())
            report = keycheck.analyze(ctx.cat, events, ctx.config["game_dir"])
        except keycheck.KeyCheckError as exc:
            return err(str(exc))
        report["file"] = f.filename
        last_report["report"] = report
        return jsonify(report)

    @app.post("/api/keycheck/apply")
    def keycheck_apply():
        if jobs.busy():
            return err("wait until the running job finishes")
        report = last_report.get("report")
        if not report:
            return err("import a Process Monitor log first")
        result = keycheck.apply(ctx.cat, report)
        last_report.clear()
        return jsonify({**result, "counts": key_counts()})

    # ---- maintenance (M6) -------------------------------------------------------------------
    @app.get("/api/disk")
    def disk():
        return jsonify({**disk_usage(ctx.cat), "cleanable": cleanup(ctx.cat, dry_run=True),
                        "busy": jobs.busy()})

    @app.post("/api/cleanup")
    def do_cleanup():
        if jobs.busy():
            return err("wait until the running job finishes before cleaning up")
        cats = (request.get_json(force=True) or {}).get("categories") or []
        try:
            return jsonify({"freed": cleanup(ctx.cat, cats)})
        except ValueError as exc:
            return err(str(exc))

    return app


def free_port(start):
    """First port from `start` that nothing is listening on (127.0.0.1)."""
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    sys.exit(f"no free port between {start} and {start + 19}")


def browse_dialog(kind, initial, title):
    """Open a native Windows file/folder dialog on this PC (the server is local) and return the path."""
    import tkinter as tk
    from tkinter import filedialog
    with _browse_lock:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        start = initial if initial and os.path.exists(initial) else None
        if kind == "file":
            path = filedialog.askopenfilename(parent=root, title=title, initialdir=start and os.path.dirname(start),
                                              filetypes=[("upscayl-bin", "*.exe"), ("All files", "*.*")])
        else:
            path = filedialog.askdirectory(parent=root, title=title, initialdir=start, mustexist=False)
        root.destroy()
    return os.path.normpath(path) if path else None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    app = create_app()
    port = free_port(a.port)
    if port != a.port:
        print(f"port {a.port} is busy (is the app already running?) - using {port}")
    a.port = port
    url = f"http://127.0.0.1:{a.port}/"
    print(f"DC2 Upscale Manager running at {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=a.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
