"""Workspace disk usage and clean-up.

Never touched by clean-up: originals\\, thumbs of originals, catalog.sqlite, approved\\, install_backup\\
(left over from older versions), run outputs still waiting for review, and anything that belongs to a run
that is queued or running. (The install record lives in the install target, not in the workspace.)

Clean-up categories (each optional):
  rejected       rejected upscales kept on disk ("delete rejected files" was unticked)
  approved_runs  run copies of approved upscales (approved\\ holds its own copy)
  run_inputs     runs\\<id>\\in\\ of finished runs (hard links to originals; resume does not need them)
  previews       thumbs\\candidates\\ preview cache (rebuilt on demand)
  staging        staging\\ (rebuilt by every install)
Run folders left with nothing but items.json keep it (needed to list leftovers / resume).
"""
import os
import shutil

CATEGORIES = ("rejected", "approved_runs", "run_inputs", "previews", "staging")
ACTIVE_RUN_STATES = ("queued", "running")


def _walk_size(path, skip_hardlinks=False):
    total = count = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            count += 1
            if not (skip_hardlinks and st.st_nlink > 1):
                total += st.st_size
    return total, count


def disk_usage(cat):
    """Bytes and file counts per workspace area. Hard-linked run inputs are counted as 0 bytes."""
    ws = cat.workspace
    areas = [("originals", "originals", False), ("thumbs", "thumbs", False), ("approved", "approved", False),
             ("staging", "staging", False), ("install_backup", "install_backup", False)]
    out = []
    for name, rel, skip in areas:
        size, n = _walk_size(os.path.join(ws, rel), skip)
        out.append({"area": name, "bytes": size, "files": n})
    runs_in = runs_out = (0, 0)
    runs_dir = os.path.join(ws, "runs")
    if os.path.isdir(runs_dir):
        si = so = ni = no = 0
        for d in os.listdir(runs_dir):
            a, b = _walk_size(os.path.join(runs_dir, d, "in"), skip_hardlinks=True)
            c, e = _walk_size(os.path.join(runs_dir, d, "out"))
            si, ni, so, no = si + a, ni + b, so + c, no + e
        runs_in, runs_out = (si, ni), (so, no)
    out.insert(2, {"area": "runs (upscaled outputs)", "bytes": runs_out[0], "files": runs_out[1]})
    out.insert(3, {"area": "runs (input links)", "bytes": runs_in[0], "files": runs_in[1]})
    db = os.path.join(ws, cat.FILENAME)
    out.append({"area": "catalog", "bytes": os.path.getsize(db) if os.path.isfile(db) else 0, "files": 1})
    return {"workspace": ws, "areas": out, "total_bytes": sum(a["bytes"] for a in out)}


def _plan(cat):
    """Files each category would delete: {category: [(abs_path, bytes), ...]}."""
    ws = cat.workspace
    active = {r["id"] for r in cat.runs() if r["state"] in ACTIVE_RUN_STATES}
    plan = {c: [] for c in CATEGORIES}

    def add(cat_name, path):
        if os.path.isfile(path):
            st = os.stat(path)
            plan[cat_name].append((path, 0 if st.st_nlink > 1 else st.st_size))

    # An approved upscale's run copy is only redundant once approved\ really holds its own copy.
    safe_items = {it["id"] for it in cat.items(status="approved")
                  if it["approved_png"] and os.path.isfile(os.path.join(ws, it["approved_png"]))}
    reopened = {it["id"] for it in cat.items() if it["status"] != "approved"}
    for c in cat.candidates():
        if c["run_id"] in active:
            continue
        path = os.path.join(ws, c["path"])
        if c["verdict"] == "rejected":
            add("rejected", path)
        elif c["verdict"] == "approved" and (c["item_id"] in safe_items or c["item_id"] in reopened):
            add("approved_runs", path)
    for r in cat.runs():
        if r["state"] in ACTIVE_RUN_STATES or not r["params"].get("dir"):
            continue
        for root, _, files in os.walk(os.path.join(ws, r["params"]["dir"], "in")):
            for f in files:
                add("run_inputs", os.path.join(root, f))
    for sub, cat_name in ((os.path.join("thumbs", "candidates"), "previews"), ("staging", "staging")):
        for root, _, files in os.walk(os.path.join(ws, sub)):
            for f in files:
                add(cat_name, os.path.join(root, f))
    return plan


def cleanup(cat, categories=CATEGORIES, dry_run=False):
    """Delete the chosen categories. Returns {category: {"files": n, "bytes": b}} (what was / would be freed)."""
    unknown = set(categories) - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown clean-up categories: {sorted(unknown)}")
    plan = _plan(cat)
    result = {}
    for c in CATEGORIES:
        entries = plan[c] if c in categories else []
        result[c] = {"files": len(entries), "bytes": sum(b for _, b in entries)}
        if dry_run:
            continue
        for path, _ in entries:
            try:
                os.remove(path)
            except OSError:
                pass
    if not dry_run:
        _prune_empty_dirs(cat)
    return result


def _prune_empty_dirs(cat):
    ws = cat.workspace
    for sub in ("runs", os.path.join("thumbs", "candidates"), "staging"):
        top = os.path.join(ws, sub)
        for root, dirs, files in os.walk(top, topdown=False):
            if root != top and not os.listdir(root):
                shutil.rmtree(root, ignore_errors=True)
