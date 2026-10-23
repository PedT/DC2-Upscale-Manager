"""Milestone M3 tests: web API (Flask test client) + background job queue, using the fake upscayl.

Run:  python -I -m unittest discover -s tests -v      (from the Upscaler folder)
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from app import create_app  # noqa: E402
from test_m2 import FAKE, make_workspace  # noqa: E402

GAME_DIR = r"C:\GOG Games\Dino Crisis 2"


class AppBase(unittest.TestCase):
    def setUp(self):
        os.environ.pop("FAKE_DELAY", None)
        self.ws, cat, self.models = make_workspace(["A", "B", "C", "D"])
        cat.close()
        self.cfg_dir = tempfile.mkdtemp(prefix="dc2_cfg_")
        self.cfg = os.path.join(self.cfg_dir, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump({"workspace": self.ws, "game_dir": GAME_DIR, "mod_dir": "",
                       "upscayl_bin": FAKE, "models_dir": self.models}, fh)
        self.app = create_app(self.cfg, log=lambda m: None)
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("FAKE_DELAY", None)
        self.wait_idle()
        self.app.config["CTX"].cat.close()
        shutil.rmtree(self.ws, ignore_errors=True)
        shutil.rmtree(self.cfg_dir, ignore_errors=True)

    def wait_idle(self, timeout=30):
        jobs = self.app.config["JOBS"]
        end = time.time() + timeout
        while jobs.busy() and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(jobs.busy(), "jobs did not finish")

    def code(self, path):
        r = self.client.get(path)
        r.close()                                   # file responses stay open until closed
        return r.status_code

    def status(self, item_id):
        return self.app.config["CTX"].cat.get_item(item_id)["status"]


class TestApi(AppBase):
    def test_index_and_state(self):
        self.assertEqual(self.code("/"), 200)
        st = self.client.get("/api/state").get_json()
        self.assertEqual(st["counts"]["room"]["todo"], 4)
        self.assertEqual(st["models"], ["fake-4x", "other-4x"])
        self.assertTrue(st["upscayl_ok"])

    def test_items(self):
        items = self.client.get("/api/items").get_json()
        self.assertEqual(sorted(i["id"] for i in items), ["A", "B", "C", "D"])
        self.assertIn("thumb", items[0])

    def test_workspace_files_are_sandboxed(self):
        self.assertEqual(self.code("/ws/originals/rooms/A.png"), 200)
        self.assertEqual(self.code("/ws/catalog.sqlite"), 404)
        self.assertEqual(self.code("/ws/../config.json"), 404)
        self.assertEqual(self.code("/ws/..%2F..%2Fsecret.png"), 404)

    def test_config_guards(self):
        r = self.client.post("/api/config", json={"workspace": os.path.join(GAME_DIR, "ws")})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/config", json={"game_dir": r"C:\definitely\not\here"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/config", json={"mod_dir": os.path.join(GAME_DIR, "Data")})
        self.assertEqual(r.status_code, 400)                              # a folder inside the game: refused
        r = self.client.post("/api/config", json={"mod_dir": os.path.join(self.cfg_dir, "mod")})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("no Dino2.exe" in w for w in r.get_json()["warnings"]))

    def test_run_end_to_end(self):
        r = self.client.post("/api/run", json={"item_ids": ["A", "B", "C"], "model": "fake-4x"})
        self.assertEqual(r.status_code, 200, r.get_json())
        run_id = r.get_json()["run_id"]
        self.wait_idle()
        self.assertEqual([self.status(i) for i in "ABC"], ["review"] * 3)
        self.assertEqual(self.status("D"), "todo")
        st = self.client.get("/api/state").get_json()
        ev = [e for e in st["jobs"]["events"] if e["kind"] == "run_done"]
        self.assertEqual((ev[-1]["run_id"], ev[-1]["done"]), (run_id, 3))
        runs = self.client.get("/api/runs").get_json()
        self.assertEqual((runs[0]["id"], runs[0]["state"], runs[0]["leftovers"]), (run_id, "done", 0))
        cand = self.app.config["CTX"].cat.candidates(run_id=run_id)[0]["path"]
        self.assertEqual(self.code("/ws/" + cand), 200)

    def test_run_rejects_bad_input(self):
        r = self.client.post("/api/run", json={"item_ids": ["A"], "model": "nope-4x"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/run", json={"item_ids": [], "model": "fake-4x"})
        self.assertEqual(r.status_code, 400)

    def test_cancel_queued_and_running(self):
        os.environ["FAKE_DELAY"] = "0.6"
        first = self.client.post("/api/run", json={"item_ids": ["A", "B"], "model": "fake-4x"}).get_json()
        second = self.client.post("/api/run", json={"item_ids": ["C", "D"], "model": "fake-4x"}).get_json()
        # Second run is still waiting in the queue: cancelling it returns its items to 'todo' at once.
        r = self.client.post(f"/api/jobs/{second['job']['id']}/cancel")
        self.assertEqual(r.get_json()["result"], "cancelled")
        self.assertEqual([self.status(i) for i in "CD"], ["todo", "todo"])
        # Cancel the running one too.
        time.sleep(0.3)
        r = self.client.post(f"/api/jobs/{first['job']['id']}/cancel")
        self.assertEqual(r.status_code, 200)
        self.wait_idle()
        runs = {r["id"]: r for r in self.client.get("/api/runs").get_json()}
        self.assertEqual(runs[first["run_id"]]["state"], "cancelled")
        self.assertEqual(runs[second["run_id"]]["state"], "cancelled")
        self.assertTrue(all(self.status(i) in ("todo", "review") for i in "AB"))

    def test_settings_locked_while_busy(self):
        os.environ["FAKE_DELAY"] = "0.5"
        self.client.post("/api/run", json={"item_ids": ["A", "B"], "model": "fake-4x"})
        r = self.client.post("/api/config", json={"mod_dir": ""})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
