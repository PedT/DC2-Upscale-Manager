"""HD mask tests: key formula vs the in-game log, atlas geometry, fallback tiles, installer integration.

Run:  python -m unittest discover -s tests -v      (from the Upscaler folder)
Uses the real game data (read-only); the key test also uses the user's Process Monitor log if present.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from core import keycheck  # noqa: E402
from core.catalog import Catalog  # noqa: E402
from core.dc2 import gogtex  # noqa: E402
from core.install import Installer  # noqa: E402
from core.masks import K, MaskSource, _place, hd_atlas  # noqa: E402

GAME_DIR = r"C:\GOG Games\Dino Crisis 2"
LOG = r"C:\Claude\DinoCrisis2ModWork\dc2_keycheck.CSV"


@unittest.skipUnless(os.path.isdir(GAME_DIR), "game data missing")
class TestMasks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = MaskSource(GAME_DIR)

    def stand_in(self, cam):
        return Image.open(io.BytesIO(cam.jpeg)).convert("RGB").resize((1280, 960), Image.LANCZOS)

    def test_kinds(self):
        self.assertEqual(self.src.camera("ST101.DBS", 0).kind, "cells")
        self.assertEqual(self.src.camera("ST101.DBS", 3).kind, "overlay")     # chain-link fence sprite page
        self.assertEqual(self.src.camera("ST000.DBS", 0).kind, "overlay")     # boat
        self.assertEqual(self.src.camera("ST905.DBS", 0).kind, "none")

    @unittest.skipUnless(os.path.isfile(LOG), "no Process Monitor log")
    def test_keys_match_game_log(self):
        """The last lookup before each camera's background lookup is its mask texture."""
        with open(LOG, "rb") as fh:
            events = keycheck.read_procmon_csv(fh.read())
        cache, timeline = {}, []
        for e in events:
            m = keycheck.PROBE_RE.search(e["path"])
            if m and e["op"] == "CreateFile":
                timeline.append(("p", m.group(1).upper()))
                continue
            d = keycheck.DBS_RE.search(e["path"])
            if d and e["op"] == "ReadFile":
                gi = keycheck._group_offsets(GAME_DIR, d.group(1), cache).get(keycheck._offset(e["detail"]))
                if gi is not None:
                    timeline.append(("c", (d.group(1).upper(), gi)))
        checked = 0
        for i, (kind, val) in enumerate(timeline):
            if kind != "c" or checked >= 30:
                continue
            cam = self.src.camera(*val)
            if cam.kind != "cells":
                continue
            window = []
            for kind2, val2 in timeline[i + 1:]:
                if kind2 == "c":
                    break
                window.append(val2)
            self.assertIn(cam.key, window, f"{val}: computed mask key not looked up by the game")
            checked += 1
        self.assertGreater(checked, 10)

    def test_atlas_geometry(self):
        cam = self.src.camera("ST101.DBS", 0)
        hd = self.stand_in(cam)
        atlas, stats = hd_atlas(cam, hd)
        self.assertEqual((atlas.size, atlas.mode), ((2048, 2048), "RGBA"))
        self.assertEqual((stats["cells"], stats["pages"], stats["fallback_cells"]), (107, 1, 0))
        A, H = np.asarray(atlas), np.asarray(hd)
        for i in (0, 50, 106):
            tx, ty, _ = cam.recs[i]
            cx, cy = _place(i)
            inner = A[(cy + 1) * K:(cy + 17) * K, (cx + 1) * K:(cx + 17) * K]
            screen = H[(ty & 15) * 16 * K:((ty & 15) * 16 + 16) * K, tx * 16 * K:(tx * 16 + 16) * K]
            self.assertTrue((inner[..., :3] == screen).all(), f"tile {i} not cut from the HD background")
            stp = (cam.cells[i] & 0x8000) != 0
            alpha_small = np.asarray(Image.fromarray(inner[..., 3]).resize((16, 16), Image.BOX)) > 127
            self.assertGreater((alpha_small == stp[1:17, 1:17]).mean(), 0.9)
        self.assertEqual(int(A[1024:, 1024:, 3].max()), 0)                 # unused quadrant stays empty

    def test_two_pages_use_second_quadrant(self):
        cam = self.src.camera("ST100.DBS", 6)                               # 280 tiles -> 2 pages
        atlas, stats = hd_atlas(cam, self.stand_in(cam))
        A = np.asarray(atlas)
        self.assertEqual(stats["pages"], 2)
        self.assertGreater(int(A[:1024, 1024:, 3].max()), 0)                # page 1 at (256, 0) * 4
        self.assertEqual(int(A[1024:, :, 3].max()), 0)

    def test_fallback_tiles(self):
        cam = self.src.camera("ST200.DBS", 9)                              # tiles from another room state
        _, stats = hd_atlas(cam, self.stand_in(cam))
        self.assertEqual(stats["fallback_cells"], 17)


@unittest.skipUnless(os.path.isdir(GAME_DIR), "game data missing")
class TestInstallWithMasks(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="dc2_mask_")
        self.cat = Catalog(self.ws)
        src = MaskSource(GAME_DIR)
        os.makedirs(os.path.join(self.ws, "approved", "rooms"))
        rows = [("ST105.DBS#6", 6, None, "approved"), ("ST105.DBS#9", 9, "ST105.DBS#6", "todo"),
                ("ST101.DBS#3", None, None, "approved")]
        for n, (item_id, gi, dup, status) in enumerate(rows):
            dbs, g = item_id.split("#")
            cam = src.camera(dbs, int(g))
            rel = f"approved/rooms/{dbs}_{g}.png"
            Image.open(io.BytesIO(cam.jpeg)).convert("RGB").resize((1280, 960), Image.LANCZOS).save(os.path.join(self.ws, rel))
            self.cat.upsert_item({"id": item_id, "kind": "room", "label": item_id, "source_file": dbs, "grp": int(g),
                                  "slot": 1, "stage": dbs[:5], "camera": int(g), "content_sha1": f"s{n}",
                                  "key32": f"0000AA0{n}", "key16": f"0000BB0{n}", "key_status": "computed",
                                  "dup_of": dup, "placeholder": 0, "original_png": rel, "thumb": rel})
            if status == "approved":
                self.cat.db.execute("UPDATE items SET status='approved', approved_png=? WHERE id=?", (rel, item_id))
        self.cat.db.commit()
        self.mod = os.path.join(self.ws, "mod")
        os.makedirs(os.path.join(self.mod, "Data"))
        with open(os.path.join(self.mod, "Dino2.exe"), "wb") as fh:
            fh.write(b"x")
        self.keys = {i: src.camera(*i.split("#")[:1], int(i.split("#")[1])).key for i in ("ST105.DBS#6", "ST105.DBS#9")}

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.ws)

    def test_rooms_install_with_masks_and_duplicates(self):
        s = Installer(self.cat, self.mod, GAME_DIR, log=lambda m: None).install()
        self.assertEqual(s["masks"], 2)                                     # master + duplicate camera
        self.assertEqual(s["overlay_rooms"], ["ST101.DBS#3"])
        files = set(os.listdir(os.path.join(self.mod, "Data")))
        for k in self.keys.values():
            self.assertIn(f"{k}.dat", files)
            with open(os.path.join(self.mod, "Data", f"{k}.dat"), "rb") as fh:
                im = Image.open(io.BytesIO(gogtex.decrypt(fh.read())))
            self.assertEqual((im.size, im.mode), ((2048, 2048), "RGBA"))
        self.assertIn("0000AA00.dat", files)                                # master background
        self.assertTrue(self.cat.get_item("ST105.DBS#9")["installed"])      # duplicate counted via its mask
        s = Installer(self.cat, self.mod, GAME_DIR, log=lambda m: None).uninstall()
        self.assertEqual(os.listdir(os.path.join(self.mod, "Data")), [])

    def test_room_needs_game_folder(self):
        ok, blocked = Installer(self.cat, self.mod, None).classify()
        self.assertTrue(all("game folder" in why for _, why in blocked))
        self.assertEqual(ok, [])


if __name__ == "__main__":
    unittest.main()
