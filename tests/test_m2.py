"""Milestone M2 tests: Upscayl runner (fake binary for logic, real binary for one smoke test).

Run:  python -I -m unittest discover -s tests -v      (from the Upscaler folder)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image  # noqa: E402

from core.catalog import Catalog  # noqa: E402
from core.runner import Runner, RunnerError, find_upscayl, list_models, pid_alive, self_check  # noqa: E402

FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_upscayl.py")


def make_workspace(names, dup=None, placeholder=None):
    """Temp workspace with one 320x240 original per name; returns (ws, catalog, models_dir)."""
    ws = tempfile.mkdtemp(prefix="dc2_m2_")
    cat = Catalog(ws)
    os.makedirs(os.path.join(ws, "originals", "rooms"))
    for k, name in enumerate(names):
        rel = f"originals/rooms/{name}.png"
        Image.new("RGB", (320, 240), (k * 20 % 256, 80, 160)).save(os.path.join(ws, rel))
        cat.upsert_item({"id": name, "kind": "room", "label": name, "source_file": "T.DBS", "grp": k,
                         "slot": 1, "stage": "T", "camera": k, "content_sha1": f"sha{k}",
                         "key_status": "predicted", "dup_of": dup if name == "DUP" else None,
                         "placeholder": int(name == placeholder), "original_png": rel, "thumb": rel})
    models = os.path.join(ws, "models")
    os.makedirs(models)
    for m in ("fake-4x", "other-4x"):
        open(os.path.join(models, m + ".param"), "w").close()
        open(os.path.join(models, m + ".bin"), "w").close()
    return ws, cat, models


class Base(unittest.TestCase):
    names = ["A", "B", "C"]

    def setUp(self):
        self.ws, self.cat, self.models = make_workspace(self.names + ["DUP", "BLANK"], dup="A", placeholder="BLANK")
        self.runner = Runner(self.cat, FAKE, self.models, log=lambda m: None)
        for var in ("FAKE_DELAY", "FAKE_EXIT", "FAKE_QUIET"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.ws)

    def statuses(self, ids):
        return [self.cat.get_item(i)["status"] for i in ids]


class TestDiscovery(unittest.TestCase):
    def test_self_check_fake(self):
        ws, cat, models = make_workspace([])
        try:
            self.assertEqual(list_models(models), ["fake-4x", "other-4x"])
            problems = self_check(FAKE)
            self.assertFalse([p for p in problems if "flag" in p], problems)
        finally:
            cat.close()
            shutil.rmtree(ws)

    def test_pid_alive(self):
        self.assertTrue(pid_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(pid_alive(p.pid))


class TestRunner(Base):
    def test_prepare_skips_and_queues(self):
        run_id, skipped = self.runner.prepare(["A", "B", "DUP", "BLANK", "NOPE"], "fake-4x")
        self.assertEqual(sorted(i for i, _ in skipped), ["BLANK", "DUP", "NOPE"])
        self.assertEqual(self.statuses(["A", "B"]), ["queued", "queued"])
        run = self.cat.get_run(run_id)
        self.assertEqual((run["state"], run["total"]), ("queued", 2))
        with self.assertRaises(RunnerError):
            self.runner.prepare(["A"], "fake-4x")              # already queued -> nothing eligible
        with self.assertRaises(RunnerError):
            self.runner.prepare(["C"], "missing-model")

    def test_execute_success(self):
        run_id, _ = self.runner.prepare(["A", "B", "C"], "fake-4x")
        seen = []
        run = self.runner.execute(run_id, progress=seen.append)
        self.assertEqual((run["state"], run["done"], run["failed"]), ("done", 3, 0))
        self.assertEqual(self.statuses(["A", "B", "C"]), ["review"] * 3)
        cands = self.cat.candidates(run_id=run_id)
        self.assertEqual(len(cands), 3)
        for c in cands:
            with Image.open(os.path.join(self.ws, c["path"])) as im:
                self.assertEqual(im.size, (1280, 960))
            self.assertEqual((c["model"], c["verdict"]), ("fake-4x", "pending"))
        self.assertEqual(seen[-1]["done"], 3)

    def test_progress_counts_during_run_without_done_lines(self):
        """The real upscayl-bin prints no per-image 'done' line unless -v; progress must still move."""
        os.environ["FAKE_QUIET"] = "1"
        os.environ["FAKE_DELAY"] = "0.5"
        try:
            run_id, _ = self.runner.prepare(["A", "B", "C"], "fake-4x")
            seen = []
            run = self.runner.execute(run_id, progress=lambda p: seen.append(p["done"]))
        finally:
            os.environ.pop("FAKE_QUIET", None)
            os.environ.pop("FAKE_DELAY", None)
        self.assertEqual((run["state"], run["done"]), ("done", 3))
        self.assertTrue(any(0 < d < 3 for d in seen), f"no intermediate progress: {seen}")

    def test_two_workers(self):
        run_id, _ = self.runner.prepare(["A", "B", "C"], "fake-4x", workers=2)
        run = self.runner.execute(run_id)
        self.assertEqual((run["state"], run["done"]), ("done", 3))
        self.assertEqual(len(os.listdir(os.path.join(self.ws, run["params"]["dir"], "in"))), 2)

    def test_scale_2(self):
        run_id, _ = self.runner.prepare(["A"], "fake-4x", scale=2)
        self.runner.execute(run_id)
        c = self.cat.candidates(run_id=run_id)[0]
        with Image.open(os.path.join(self.ws, c["path"])) as im:
            self.assertEqual(im.size, (640, 480))


class TestFailureAndCancel(Base):
    names = ["A", "B_FAIL", "C", "D", "E"]

    def test_failed_item_returns_to_todo(self):
        run_id, _ = self.runner.prepare(["A", "B_FAIL", "C"], "fake-4x")
        run = self.runner.execute(run_id)
        self.assertEqual((run["state"], run["done"], run["failed"]), ("done", 2, 1))
        it = self.cat.get_item("B_FAIL")
        self.assertEqual(it["status"], "todo")
        self.assertIn("upscale failed", it["notes"])
        self.assertIn("B_FAIL", run["log"])

    def test_cancel(self):
        os.environ["FAKE_DELAY"] = "0.7"
        run_id, _ = self.runner.prepare(["A", "C", "D", "E"], "fake-4x")
        cancel = threading.Event()

        def stop_after_first(p):
            if p["done"] >= 1:
                cancel.set()

        run = self.runner.execute(run_id, progress=stop_after_first, cancel=cancel)
        self.assertEqual(run["state"], "cancelled")
        st = self.statuses(["A", "C", "D", "E"])
        self.assertGreaterEqual(st.count("review"), 1)
        self.assertGreaterEqual(st.count("todo"), 1)
        self.assertEqual(st.count("review") + st.count("todo"), 4)
        self.assertEqual(run["failed"], 0)                      # cancelled items are not failures


class TestRecovery(Base):
    def test_recover_and_resume(self):
        run_id, _ = self.runner.prepare(["A", "B", "C"], "fake-4x")
        run = self.cat.get_run(run_id)
        # Simulate a crash: run marked running by a dead process, A finished, B mid-upscale, C queued.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        params = dict(run["params"], pid=dead.pid)
        self.cat.update_run(run_id, state="running", params=params)
        for i in ("A", "B"):
            self.cat.set_status(i, "upscaling")
        out = os.path.join(self.ws, run["params"]["dir"], "out")
        Image.new("RGB", (1280, 960)).save(os.path.join(out, "A.png"))
        with open(os.path.join(out, "B.png"), "wb") as fh:
            fh.write(b"\x89PNG partial")                        # half-written file
        self.assertEqual(self.runner.recover(), [run_id])
        self.assertEqual(self.cat.get_run(run_id)["state"], "interrupted")
        self.assertEqual(self.statuses(["A", "B", "C"]), ["review", "todo", "todo"])
        self.assertEqual(sorted(self.runner.leftovers(run_id)), ["B", "C"])
        new_id, _ = self.runner.resume(run_id)
        run2 = self.runner.execute(new_id)
        self.assertEqual((run2["state"], run2["done"]), ("done", 2))
        self.assertEqual(self.statuses(["A", "B", "C"]), ["review"] * 3)

    def test_live_run_not_recovered(self):
        run_id, _ = self.runner.prepare(["A"], "fake-4x")      # queued by this (live) process
        self.assertEqual(self.runner.recover(), [])
        self.assertEqual(self.cat.get_run(run_id)["state"], "queued")


@unittest.skipUnless(find_upscayl(), "Upscayl not installed")
class TestRealUpscayl(unittest.TestCase):
    """One real image through the installed upscayl-bin (GPU), ~5 s."""

    def test_one_image(self):
        bin_path = find_upscayl()
        self.assertEqual(self_check(bin_path), [])
        ws, cat, _ = make_workspace(["REAL"])
        try:
            models = os.path.join(os.path.dirname(os.path.dirname(bin_path)), "models")
            model = "upscayl-lite-4x" if "upscayl-lite-4x" in list_models(models) else list_models(models)[0]
            runner = Runner(cat, bin_path, log=lambda m: None)
            run_id, _ = runner.prepare(["REAL"], model)
            t0 = time.time()
            run = runner.execute(run_id)
            self.assertEqual((run["state"], run["done"]), ("done", 1), run["log"])
            c = cat.candidates(run_id=run_id)[0]
            with Image.open(os.path.join(ws, c["path"])) as im:
                self.assertEqual(im.size, (1280, 960))
            self.assertLess(time.time() - t0, 120)
        finally:
            cat.close()
            shutil.rmtree(ws)


if __name__ == "__main__":
    unittest.main()
