"""Install tests: install / sync / uninstall into an install target with its own record file.

Run:  python -m unittest discover -s tests -v      (from the Upscaler folder)
Every test uses temporary fake game folders; nothing here writes to the real GOG install.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from PIL import Image  # noqa: E402

from core.dc2 import gogtex  # noqa: E402
from core.extract import find_fullscreen  # noqa: E402
from core.install import RECORD, Installer, game_color_depth, installed_names  # noqa: E402
from core.paths import PathError  # noqa: E402
from core.review import Review  # noqa: E402
from core.runner import Runner  # noqa: E402
from test_m2 import FAKE, make_workspace  # noqa: E402
from test_m3 import AppBase  # noqa: E402

KEYS = {"T1": ("AAAA0001", "BBBB0001", "computed"), "T2": ("AAAA0002", "BBBB0002", "verified"),
        "R1": ("AAAA0003", "BBBB0003", "predicted")}
REAL_GAME = r"C:\GOG Games\Dino Crisis 2"


def set_keys(cat):
    for item_id, (k32, k16, status) in KEYS.items():
        kind = "room" if status == "predicted" else "fullscreen"
        cat.db.execute("UPDATE items SET key32=?, key16=?, key_status=?, kind=? WHERE id=?",
                       (k32, k16, status, kind, item_id))
    cat.db.commit()


def make_game(root, foreign=None):
    """A stand-in game folder: Dino2.exe, Data\\ with GOG's own fix file and optional foreign files."""
    game = os.path.join(root, "game")
    os.makedirs(os.path.join(game, "Data"))
    with open(os.path.join(game, "Dino2.exe"), "wb") as fh:
        fh.write(b"not the real exe")
    with open(os.path.join(game, "Data", "9A8B79FC.DAT"), "wb") as fh:
        fh.write(b"GOG fix")
    for name, data in (foreign or {}).items():
        with open(os.path.join(game, "Data", name), "wb") as fh:
            fh.write(data)
    return game


make_mod = make_game          # older name, still used by other test modules


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class InstallBase(unittest.TestCase):
    foreign = None

    def setUp(self):
        self.ws, self.cat, models = make_workspace(["T1", "T2", "R1"])
        set_keys(self.cat)
        runner = Runner(self.cat, FAKE, models, log=lambda m: None)
        run_id, _ = runner.prepare(["T1", "T2", "R1"], "fake-4x")
        runner.execute(run_id)
        Review(self.cat).batch("approve", ["T1", "T2", "R1"])
        self.tmp = tempfile.mkdtemp(prefix="dc2_inst_")
        self.game = make_game(self.tmp, self.foreign)
        # Target == game folder: the "install straight into the game" mode.
        # (16-bit files included here so the tests see both keys; the app's default is 32-bit only.)
        self.inst = Installer(self.cat, self.game, self.game, log=lambda m: None, include_16bit=True)

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.ws)
        shutil.rmtree(self.tmp)

    def data(self, name):
        return os.path.join(self.game, "Data", name)

    def record(self):
        path = os.path.join(self.game, RECORD)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)


class TestInstaller(InstallBase):
    def test_target_checks(self):
        self.assertTrue(any("GOG English" in w for w in self.inst.check_target()))
        self.assertEqual(self.inst.target_kind(), "game")
        self.assertEqual(Installer(self.cat, self.game, None).target_kind(), "copy")
        for bad in ("", os.path.join(self.tmp, "nope"), os.path.join(self.game, "Data"),
                    r"C:\GOG Games\Some Other Game"):
            with self.assertRaises(PathError):
                Installer(self.cat, bad, self.game).check_target()

    def test_install_writes_files_and_record(self):
        s = self.inst.install()
        self.assertEqual((s["added"], s["updated"], s["items"], s["target_is_game"]), (4, 0, 2, True))
        self.assertEqual([b[0] for b in s["blocked"]], ["R1"])                   # predicted key
        for name in ("AAAA0001.dat", "BBBB0001.dat", "AAAA0002.dat", "BBBB0002.dat"):
            with Image.open(io.BytesIO(gogtex.decrypt(read(self.data(name))))) as im:
                self.assertEqual(im.size, (2048, 1024))
        self.assertFalse(os.path.exists(self.data("AAAA0003.dat")))
        self.assertEqual(read(self.data("9A8B79FC.DAT")), b"GOG fix")
        rec = self.record()
        self.assertEqual(sorted(rec["files"]), ["AAAA0001.dat", "AAAA0002.dat", "BBBB0001.dat", "BBBB0002.dat"])
        self.assertEqual(rec["files"]["AAAA0002.dat"]["item_id"], "T2")
        self.assertEqual(installed_names(self.game), {"AAAA0001.DAT", "AAAA0002.DAT", "BBBB0001.DAT", "BBBB0002.DAT"})
        self.assertTrue(self.cat.get_item("T1")["installed"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "install_manifest.json")))

    def test_reinstall_is_idempotent(self):
        self.inst.install()
        s = self.inst.install()
        self.assertEqual((s["added"], s["updated"], s["unchanged"], s["removed"]), (0, 0, 4, 0))

    def test_sync_removes_reopened(self):
        self.inst.install()
        Review(self.cat).reopen("T2")
        s = self.inst.install()
        self.assertEqual(s["removed"], 2)
        self.assertFalse(os.path.exists(self.data("AAAA0002.dat")))
        self.assertEqual(sorted(self.record()["files"]), ["AAAA0001.dat", "BBBB0001.dat"])
        self.assertFalse(self.cat.get_item("T2")["installed"])

    def test_uninstall_leaves_folder_as_it_was(self):
        before = sorted(os.listdir(self.game)), sorted(os.listdir(os.path.join(self.game, "Data")))
        self.inst.install()
        s = self.inst.uninstall()
        self.assertEqual((s["removed"], s["kept_modified"]), (4, []))
        after = sorted(os.listdir(self.game)), sorted(os.listdir(os.path.join(self.game, "Data")))
        self.assertEqual(after, before)                                       # record file gone too
        self.assertFalse(any(it["installed"] for it in self.cat.items()))

    def test_uninstall_keeps_files_changed_by_hand(self):
        self.inst.install()
        with open(self.data("BBBB0002.dat"), "wb") as fh:
            fh.write(b"user changed this")
        s = self.inst.uninstall()
        self.assertEqual((s["removed"], s["kept_modified"]), (3, ["BBBB0002.dat"]))
        self.assertEqual(read(self.data("BBBB0002.dat")), b"user changed this")
        self.assertEqual(sorted(self.record()["files"]), ["BBBB0002.dat"])     # still listed

    def test_edited_image_reinstalls(self):
        self.inst.install()
        before = read(self.data("AAAA0001.dat"))
        buf = io.BytesIO()
        Image.new("RGB", (1280, 960), (255, 0, 0)).save(buf, "PNG")
        Review(self.cat).replace_approved("T1", buf.getvalue())
        s = self.inst.install()
        self.assertEqual(s["updated"], 2)
        self.assertNotEqual(read(self.data("AAAA0001.dat")), before)

    def test_interrupted_install_adopts_its_own_identical_files(self):
        self.inst.install()
        os.remove(os.path.join(self.game, RECORD))                           # e.g. a crash before the record
        s = self.inst.install()
        self.assertEqual((s["unchanged"], s["skipped_existing"]), (4, []))
        self.assertEqual(len(self.record()["files"]), 4)

    def test_32bit_only_and_switching_off_16bit(self):
        self.inst.install()                                                   # with 16-bit files
        only32 = Installer(self.cat, self.game, self.game, log=lambda m: None)   # default: 32-bit only
        s = only32.install()
        self.assertEqual((s["removed"], s["unchanged"], s["include_16bit"]), (2, 2, False))
        self.assertEqual(sorted(self.record()["files"]), ["AAAA0001.dat", "AAAA0002.dat"])
        self.assertFalse(os.path.exists(self.data("BBBB0001.dat")))

    def test_color_depth(self):
        self.assertIsNone(game_color_depth(self.game))                         # no dino2.cfg
        for depth in (32, 16):
            with open(os.path.join(self.game, "dino2.cfg"), "wb") as fh:
                fh.write(bytes([1, 0, 1, 0, 0x00, 0x0a, 0xa0, 0x05, depth, 0]) + bytes(20))
            self.assertEqual(game_color_depth(self.game), depth)
            self.assertEqual(self.inst.status()["color_depth"], depth)

    def test_extraction_skips_installed_files(self):
        self.inst.install()
        with open(self.data("TEST.PXL"), "wb") as fh:
            fh.write(bytes(320 * 240 * 2))
        found = [f["id"] for f in find_fullscreen(os.path.join(self.game, "Data"), log=lambda m: None,
                                                  ignore=installed_names(self.game))]
        self.assertEqual(found, ["TEST.PXL"])


class TestForeignFiles(InstallBase):
    foreign = {"AAAA0001.dat": b"somebody else's file"}

    def test_never_overwrites_files_it_did_not_create(self):
        s = self.inst.install()
        self.assertEqual(s["skipped_existing"], ["AAAA0001.dat"])
        self.assertEqual(read(self.data("AAAA0001.dat")), b"somebody else's file")
        self.assertNotIn("AAAA0001.dat", self.record()["files"])
        self.inst.uninstall()
        self.assertEqual(read(self.data("AAAA0001.dat")), b"somebody else's file")
        self.assertEqual(sorted(os.listdir(os.path.join(self.game, "Data"))), ["9A8B79FC.DAT", "AAAA0001.dat"])


class TestLegacyRecord(InstallBase):
    def test_old_workspace_record_moves_into_its_folder(self):
        copy = make_game(os.path.join(self.tmp, "copy"))
        with open(os.path.join(copy, "Data", "CCCC0001.dat"), "wb") as fh:
            fh.write(b"installed by an older version")
        legacy = {"mod_dir": copy, "files": {"CCCC0001.dat": {
            "item_id": "T1", "label": "T1", "sha1": __import__("hashlib").sha1(b"installed by an older version").hexdigest(),
            "size": 29, "texture": "2048x1024", "installed": 0, "backup": None}}}
        with open(os.path.join(self.ws, "install_manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(legacy, fh)
        self.inst.install()                                                   # installing into the game folder
        with open(os.path.join(copy, RECORD), encoding="utf-8") as fh:
            moved = json.load(fh)
        self.assertEqual(list(moved["files"]), ["CCCC0001.dat"])              # the copy now describes itself
        self.assertTrue(os.path.isfile(os.path.join(copy, "Data", "CCCC0001.dat")))   # and was left alone
        self.assertFalse(os.path.exists(os.path.join(self.ws, "install_manifest.json")))
        Installer(self.cat, copy, self.game, log=lambda m: None).uninstall()   # the copy can still be cleaned
        self.assertFalse(os.path.exists(os.path.join(copy, "Data", "CCCC0001.dat")))


class TestInstallApi(AppBase):
    def test_install_and_uninstall_via_api(self):
        ctx = self.app.config["CTX"]
        st = self.client.get("/api/install").get_json()
        self.assertIn("no install target", st["error"])
        self.assertEqual(self.client.post("/api/install").status_code, 400)
        game = make_game(self.cfg_dir)
        self.assertEqual(self.client.post("/api/config", json={"mod_dir": game}).status_code, 200)
        cat = ctx.cat
        cat.db.execute("UPDATE items SET key32='CCCC0001', key16='DDDD0001', key_status='computed', kind='fullscreen' WHERE id='A'")
        cat.db.commit()
        runner = Runner(cat, FAKE, self.models, log=lambda m: None)
        run_id, _ = runner.prepare(["A"], "fake-4x")
        runner.execute(run_id)
        self.client.post("/api/review/approve", json={"item_ids": ["A"]})
        st = self.client.get("/api/install").get_json()
        self.assertEqual((st["error"], st["installable"]), (None, ["A"]))
        self.assertEqual(self.client.post("/api/install").status_code, 200)
        self.wait_idle()
        self.assertTrue(os.path.isfile(os.path.join(game, "Data", "CCCC0001.dat")))
        st = self.client.get("/api/install").get_json()
        self.assertEqual(sorted(st["installed_files"]), ["CCCC0001.dat"])          # default: 32-bit only
        self.assertEqual(self.client.post("/api/config", json={"install_16bit": True}).status_code, 200)
        self.client.post("/api/install")
        self.wait_idle()
        st = self.client.get("/api/install").get_json()
        self.assertEqual(sorted(st["installed_files"]), ["CCCC0001.dat", "DDDD0001.dat"])
        self.assertTrue(st["include_16bit"])
        self.client.post("/api/uninstall")
        self.wait_idle()
        self.assertFalse(os.path.exists(os.path.join(game, "Data", "CCCC0001.dat")))
        self.assertFalse(os.path.exists(os.path.join(game, RECORD)))
        events = [e["kind"] for e in self.client.get("/api/state").get_json()["jobs"]["events"]]
        self.assertIn("install_done", events)
        self.assertIn("uninstall_done", events)

    def test_game_folder_is_an_accepted_target(self):
        r = self.client.post("/api/config", json={"mod_dir": REAL_GAME})           # only validated, nothing written
        self.assertEqual(r.status_code, 200, r.get_json())
        for bad in (os.path.join(REAL_GAME, "Data"), r"C:\GOG Games\Some Other Game"):
            self.assertEqual(self.client.post("/api/config", json={"mod_dir": bad}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
