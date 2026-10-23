"""Milestone M6 tests: disk usage, clean-up safety, free-port fallback.

Run:  python -m unittest discover -s tests -v      (from the Upscaler folder)
"""
import os
import socket
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from app import free_port  # noqa: E402
from core.maintenance import cleanup, disk_usage  # noqa: E402
from core.review import Review  # noqa: E402
from test_m2 import Base  # noqa: E402
from test_m3 import AppBase  # noqa: E402


class TestMaintenance(Base):
    names = ["A", "B", "C", "D"]

    def setUp(self):
        super().setUp()
        self.review = Review(self.cat)
        run_id, _ = self.runner.prepare(list("ABCD"), "fake-4x")
        self.runner.execute(run_id)
        self.cand = {i: self.review.pending_candidate(i)["path"] for i in "ABCD"}
        self.review.approve("A")
        self.review.reject("B", delete_file=False)
        self.review.approve("D")
        self.review.reopen("D")                                    # C stays waiting for review
        self.review.candidate_thumb(self.cand["C"])
        self.run_dir = os.path.join(self.ws, self.cat.get_run(run_id)["params"]["dir"])

    def exists(self, rel):
        return os.path.isfile(os.path.join(self.ws, rel))

    def test_disk_usage(self):
        d = disk_usage(self.cat)
        areas = {a["area"]: a for a in d["areas"]}
        self.assertEqual(areas["runs (input links)"]["bytes"], 0)         # hard links cost nothing
        self.assertEqual(areas["runs (input links)"]["files"], 4)
        self.assertEqual(areas["runs (upscaled outputs)"]["files"], 4)
        self.assertGreater(areas["approved"]["bytes"], 0)
        self.assertEqual(d["total_bytes"], sum(a["bytes"] for a in d["areas"]))

    def test_dry_run_counts_and_deletes_nothing(self):
        r = cleanup(self.cat, dry_run=True)
        self.assertEqual({k: v["files"] for k, v in r.items()},
                         {"rejected": 1, "approved_runs": 2, "run_inputs": 4, "previews": 1, "staging": 0})
        self.assertTrue(all(self.exists(p) for p in self.cand.values()))

    def test_cleanup_keeps_what_matters(self):
        approved = self.cat.get_item("A")["approved_png"]
        cleanup(self.cat)
        self.assertFalse(self.exists(self.cand["B"]))                      # rejected
        self.assertFalse(self.exists(self.cand["A"]))                      # run copy of approved
        self.assertFalse(self.exists(self.cand["D"]))                      # reopened
        self.assertTrue(self.exists(self.cand["C"]))                       # still waiting for review
        self.assertTrue(self.exists(approved))
        for i in "ABCD":
            self.assertTrue(self.exists(self.cat.get_item(i)["original_png"]))
        self.assertTrue(os.path.isfile(os.path.join(self.run_dir, "items.json")))
        self.assertFalse(os.path.isdir(os.path.join(self.run_dir, "in")))
        left = [f for _, _, fs in os.walk(os.path.join(self.ws, "thumbs", "candidates")) for f in fs]
        self.assertEqual(left, [])                                           # preview cache emptied
        self.assertEqual(self.cat.get_item("C")["status"], "review")
        self.assertEqual(cleanup(self.cat, dry_run=True)["rejected"]["files"], 0)

    def test_only_selected_categories(self):
        cleanup(self.cat, ["rejected"])
        self.assertFalse(self.exists(self.cand["B"]))
        self.assertTrue(self.exists(self.cand["A"]))
        with self.assertRaises(ValueError):
            cleanup(self.cat, ["everything"])

    def test_active_run_untouched(self):
        run2, _ = self.runner.prepare(["B"], "fake-4x")                    # queued, not executed
        cleanup(self.cat)
        in_dir = os.path.join(self.ws, self.cat.get_run(run2)["params"]["dir"], "in")
        self.assertEqual(sum(len(f) for _, _, f in os.walk(in_dir)), 1)


class TestMaintenanceApi(AppBase):
    def test_disk_and_cleanup(self):
        d = self.client.get("/api/disk").get_json()
        self.assertIn("cleanable", d)
        self.assertFalse(d["busy"])
        r = self.client.post("/api/cleanup", json={"categories": ["previews", "staging"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/api/cleanup", json={"categories": ["bogus"]}).status_code, 400)

    def test_cleanup_refused_while_busy(self):
        os.environ["FAKE_DELAY"] = "0.5"
        self.client.post("/api/run", json={"item_ids": ["A", "B"], "model": "fake-4x"})
        self.assertEqual(self.client.post("/api/cleanup", json={"categories": ["staging"]}).status_code, 400)


class TestFreePort(unittest.TestCase):
    def test_skips_busy_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            busy = s.getsockname()[1]
            self.assertNotEqual(free_port(busy), busy)


if __name__ == "__main__":
    unittest.main()
