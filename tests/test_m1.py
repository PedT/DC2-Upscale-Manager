"""Milestone M1 tests: path guards, catalog workflow, full extraction against the real game data.

Run:  python -I -m unittest discover -s tests -v      (from the Upscaler folder)
The extraction tests need the GOG install at GAME_DIR (read-only) and are skipped without it.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PIL import Image  # noqa: E402

from core.catalog import Catalog, TransitionError  # noqa: E402
from core.extract import extract  # noqa: E402
from core.paths import PathError, check_game_dir, check_writable_target  # noqa: E402

GAME_DIR = os.environ.get("DC2_GAME_DIR", r"C:\GOG Games\Dino Crisis 2")
PREDICTED = r"C:\Claude\DinoCrisis2ModWork\work\wf\rooms\predicted_wrapper_crcs.json"

# Keys known from the research (ED666D62 confirmed in game; the rest match Rex-HD's file names).
EXPECTED_KEYS = {
    "TITLE.DAT#5": ("ED666D62", "127F7F54", "verified"),
    "TITLE2.DAT#7": ("B6F657BD", "B24A911C", "computed"),
    "CAPLOGO.PXL": ("95EA9776", "504CA786", "computed"),
    "WARNINGE.PXL": ("826468D4", "ECE2DAB7", "computed"),
    "M_TITLE.PXL": ("3A0EA3F2", "38113737", "computed"),
    "CONTINUE.DAT#3": ("56DB75A7", None, "computed"),
    "GAMEOVER.DAT#0": ("91980609", None, "computed"),
}


def sample_item(item_id="X#1", sha="aaa"):
    return {"id": item_id, "kind": "room", "label": "test", "source_file": "X.DBS", "grp": 1, "slot": 1,
            "stage": "X", "camera": 1, "content_sha1": sha, "key32": "00000000", "key16": "00000000",
            "key_status": "predicted", "dup_of": None, "placeholder": 0,
            "original_png": "originals/rooms/x.png", "thumb": "thumbs/rooms/x.jpg"}


class TestPaths(unittest.TestCase):
    def test_refuses_game_folder_and_gog_root(self):
        with self.assertRaises(PathError):
            check_writable_target(os.path.join(GAME_DIR, "Data"), GAME_DIR)
        with self.assertRaises(PathError):
            check_writable_target(r"C:\GOG Games\Other", None)
        check_writable_target(r"C:\Claude\somewhere", GAME_DIR)       # allowed

    def test_rejects_non_install(self):
        tmp = tempfile.mkdtemp()
        try:
            with self.assertRaises(PathError):
                check_game_dir(tmp)
        finally:
            shutil.rmtree(tmp)


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.cat = Catalog(self.ws)
        self.cat.upsert_item(sample_item())

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.ws)

    def test_happy_path_and_reject(self):
        c = self.cat
        for s in ("queued", "upscaling", "review"):
            c.set_status("X#1", s)
        c.set_status("X#1", "todo", model="ultrasharp-4x")              # rejected
        self.assertEqual(c.get_item("X#1")["models_tried"], ["ultrasharp-4x"])
        for s in ("queued", "upscaling", "review"):
            c.set_status("X#1", s)
        c.set_status("X#1", "approved", approved_png="approved/x.png")
        it = c.get_item("X#1")
        self.assertEqual((it["status"], it["approved_png"]), ("approved", "approved/x.png"))
        c.set_status("X#1", "todo")                                     # reopened
        self.assertIsNone(c.get_item("X#1")["approved_png"])

    def test_illegal_transitions(self):
        with self.assertRaises(TransitionError):
            self.cat.set_status("X#1", "approved")                      # todo -> approved
        with self.assertRaises(TransitionError):
            self.cat.set_status("X#1", "nonsense")

    def test_upsert_keeps_or_resets_state(self):
        c = self.cat
        c.set_status("X#1", "queued")
        self.assertEqual(c.upsert_item(sample_item()), "unchanged")
        self.assertEqual(c.get_item("X#1")["status"], "queued")
        self.assertEqual(c.upsert_item(sample_item(sha="bbb")), "changed")
        self.assertEqual(c.get_item("X#1")["status"], "todo")

    def test_settings_roundtrip(self):
        self.cat.set_setting("game_dir", r"C:\x")
        self.assertEqual(self.cat.get_setting("game_dir"), r"C:\x")
        self.assertEqual(self.cat.get_setting("missing", 5), 5)


@unittest.skipUnless(os.path.isdir(GAME_DIR), f"game install not found at {GAME_DIR}")
class TestExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ws = tempfile.mkdtemp(prefix="dc2_ws_")
        data = os.path.join(GAME_DIR, "Data")
        cls.before = {n: os.stat(os.path.join(data, n)).st_mtime_ns for n in os.listdir(data)}
        cls.summary = extract(GAME_DIR, cls.ws, log=lambda m: None)
        cls.cat = Catalog(cls.ws)

    @classmethod
    def tearDownClass(cls):
        cls.cat.close()
        shutil.rmtree(cls.ws)

    def test_counts(self):
        s = self.summary
        self.assertEqual((s["fullscreen"], s["rooms"]), (23, 1169))
        self.assertEqual((s["placeholders"], s["no_key"]), (4, 0))

    def test_fullscreen_keys(self):
        for item_id, (k32, k16, status) in EXPECTED_KEYS.items():
            it = self.cat.get_item(item_id)
            self.assertIsNotNone(it, item_id)
            self.assertEqual(it["key32"], k32, item_id)
            if k16:
                self.assertEqual(it["key16"], k16, item_id)
            self.assertEqual(it["key_status"], status, item_id)

    @unittest.skipUnless(os.path.isfile(PREDICTED), "research predictions not available")
    def test_room_keys_match_research(self):
        with open(PREDICTED, encoding="utf-8") as fh:
            pred = json.load(fh)
        rooms = self.cat.items(kind="room")
        self.assertEqual(len(rooms), len(pred))
        for it in rooms:
            p = pred[f"{it['source_file']}:{it['grp']}"]
            self.assertEqual((it["key32"], it["key16"]), (p["crc32_pc53"], p["crc16_pc53"]), it["id"])
            self.assertEqual(it["key_status"], "predicted")

    def test_images_written(self):
        for it in self.cat.items()[::50]:
            with Image.open(os.path.join(self.ws, it["original_png"])) as im:
                self.assertEqual(im.size, (320, 240))
            self.assertTrue(os.path.isfile(os.path.join(self.ws, it["thumb"])))

    def test_rerun_keeps_state(self):
        self.cat.set_status("ST100.DBS#0", "queued")
        s = extract(GAME_DIR, self.ws, log=lambda m: None)
        self.assertEqual(s["unchanged"], 23 + 1169)
        self.assertEqual(self.cat.get_item("ST100.DBS#0")["status"], "queued")
        self.cat.set_status("ST100.DBS#0", "todo")

    def test_game_folder_untouched(self):
        data = os.path.join(GAME_DIR, "Data")
        after = {n: os.stat(os.path.join(data, n)).st_mtime_ns for n in os.listdir(data)}
        self.assertEqual(after, self.before)


if __name__ == "__main__":
    unittest.main()
