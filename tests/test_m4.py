"""Milestone M4 tests: review actions (approve / reject / reopen / replace) and their web API.

Run:  python -m unittest discover -s tests -v      (from the Upscaler folder)
"""
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from PIL import Image  # noqa: E402

from core.review import Review, ReviewError  # noqa: E402
from core.runner import Runner  # noqa: E402
from test_m2 import FAKE, Base  # noqa: E402
from test_m3 import AppBase  # noqa: E402


def png_bytes(size, color=(10, 200, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class TestReview(Base):
    def setUp(self):
        super().setUp()
        self.review = Review(self.cat)
        run_id, _ = self.runner.prepare(["A", "B", "C"], "fake-4x")
        self.runner.execute(run_id)

    def cand(self, item_id):
        return self.review.pending_candidate(item_id)

    def test_queue(self):
        q = self.review.queue()
        self.assertEqual([e["item"]["id"] for e in q], ["A", "B", "C"])
        self.assertTrue(all(e["candidate"]["model"] == "fake-4x" for e in q))

    def test_approve(self):
        c = self.cand("A")
        rel = self.review.approve("A")
        it = self.cat.get_item("A")
        self.assertEqual((it["status"], it["approved_png"]), ("approved", rel))
        with open(os.path.join(self.ws, rel), "rb") as a, open(os.path.join(self.ws, c["path"]), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertEqual(self.cat.get_candidate(c["id"])["verdict"], "approved")
        with self.assertRaises(ReviewError):
            self.review.approve("A")                                # not in review any more
        with self.assertRaises(ReviewError):
            self.review.approve("nope")                             # unknown item

    def test_reject_deletes_and_records_model(self):
        c = self.cand("B")
        path = os.path.join(self.ws, c["path"])
        self.review.candidate_thumb(c["path"])
        self.review.reject("B")
        it = self.cat.get_item("B")
        self.assertEqual((it["status"], it["models_tried"]), ("todo", ["fake-4x"]))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.cat.get_candidate(c["id"])["verdict"], "rejected")

    def test_reject_keep_file_then_retry_with_other_model(self):
        c = self.cand("C")
        self.review.reject("C", delete_file=False)
        self.assertTrue(os.path.exists(os.path.join(self.ws, c["path"])))
        run_id, _ = self.runner.prepare(["C"], "other-4x")
        self.runner.execute(run_id)
        new = self.cand("C")
        self.assertEqual(new["model"], "other-4x")
        self.review.approve("C")
        self.assertEqual(self.cat.get_candidate(new["id"])["verdict"], "approved")
        self.assertEqual(self.cat.get_candidate(c["id"])["verdict"], "rejected")
        self.assertIn("other-4x", self.cat.get_item("C")["notes"])

    def test_reopen(self):
        self.review.approve("A")
        self.review.reopen("A")
        it = self.cat.get_item("A")
        self.assertEqual((it["status"], it["approved_png"]), ("todo", None))
        with self.assertRaises(ReviewError):
            self.review.reopen("B")                                 # in review, not approved

    def test_replace_approved(self):
        with self.assertRaises(ReviewError):
            self.review.replace_approved("A", png_bytes((1280, 960)))   # not approved yet
        self.review.approve("A")
        rel = self.review.replace_approved("A", png_bytes((1280, 960)), "fixed.png")
        self.assertEqual(self.cat.get_item("A")["approved_png"], rel)
        with Image.open(os.path.join(self.ws, rel)) as im:
            self.assertEqual(im.size, (1280, 960))
        for bad in (png_bytes((1600, 900)), png_bytes((160, 120)), b"not an image"):
            with self.assertRaises(ReviewError):
                self.review.replace_approved("A", bad)

    def test_candidate_thumb(self):
        c = self.cand("A")
        rel = self.review.candidate_thumb(c["path"])
        with Image.open(os.path.join(self.ws, rel)) as im:
            self.assertEqual((im.size, im.format), ((320, 240), "JPEG"))
        self.assertEqual(self.review.candidate_thumb(c["path"]), rel)   # cached

    def test_batch_reports_per_item_errors(self):
        r = self.review.batch("approve", ["A", "B", "D", "nope"])
        self.assertEqual(r["ok"], ["A", "B"])
        self.assertEqual(sorted(r["errors"]), ["D", "nope"])


class TestReviewApi(AppBase):
    def setUp(self):
        super().setUp()
        ctx = self.app.config["CTX"]
        runner = Runner(ctx.cat, FAKE, self.models, log=lambda m: None)
        run_id, _ = runner.prepare(["A", "B"], "fake-4x")
        runner.execute(run_id)

    def test_review_flow(self):
        q = self.client.get("/api/review").get_json()
        self.assertEqual([e["id"] for e in q], ["A", "B"])
        path = q[0]["candidate"]["path"]
        self.assertEqual(self.code("/cthumb/" + path), 200)
        self.assertEqual(self.code("/cthumb/../catalog.png"), 404)
        self.assertEqual(self.code("/cthumb/originals/rooms/A.png"), 404)   # only run outputs
        r = self.client.post("/api/review/approve", json={"item_ids": ["A"]}).get_json()
        self.assertEqual(r, {"ok": ["A"], "errors": {}})
        r = self.client.post("/api/review/reject", json={"item_ids": ["B"], "delete_file": False}).get_json()
        self.assertEqual(r["ok"], ["B"])
        self.assertEqual((self.status("A"), self.status("B")), ("approved", "todo"))
        self.assertEqual(self.client.get("/api/review").get_json(), [])
        self.assertEqual(self.client.post("/api/review/destroy", json={}).status_code, 404)

    def test_bulk_reopen_then_rerun(self):
        self.client.post("/api/review/approve", json={"item_ids": ["A", "B"]})
        r = self.client.post("/api/review/reopen", json={"item_ids": ["A", "B", "C"]}).get_json()
        self.assertEqual(r["ok"], ["A", "B"])
        self.assertIn("C", r["errors"])                                   # C was never approved
        self.assertEqual((self.status("A"), self.status("B")), ("todo", "todo"))
        r = self.client.post("/api/run", json={"item_ids": ["A", "B"], "model": "other-4x"})
        self.assertEqual(r.status_code, 200)
        self.wait_idle()
        q = {e["id"]: e["candidate"]["model"] for e in self.client.get("/api/review").get_json()}
        self.assertEqual(q, {"A": "other-4x", "B": "other-4x"})

    def test_replace_upload(self):
        self.client.post("/api/review/approve", json={"item_ids": ["A"]})
        r = self.client.post("/api/items/replace?id=A",
                             data={"file": (io.BytesIO(png_bytes((1280, 960))), "fixed.png")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json())
        rel = r.get_json()["approved_png"]
        self.assertEqual(self.code("/ws/" + rel), 200)
        r = self.client.post("/api/items/replace?id=A",
                             data={"file": (io.BytesIO(png_bytes((1000, 500))), "wide.png")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("shape", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
