"""In-game key check tests: Process Monitor CSV import, analysis outcomes, apply, persistence, install gating.

Run:  python -m unittest discover -s tests -v      (from the Upscaler folder)
Uses the real ST*.DBS files (read-only) for camera offsets and the research predictions for keys.
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

from core import keycheck  # noqa: E402
from core.catalog import Catalog  # noqa: E402
from core.dc2 import dc2pack as P  # noqa: E402
from core.install import Installer  # noqa: E402

GAME_DIR = r"C:\GOG Games\Dino Crisis 2"
PREDICTED = r"C:\Claude\DinoCrisis2ModWork\work\wf\rooms\predicted_wrapper_crcs.json"
MOD = r"C:\Games\DC2-mod"


def fmt_offset(n):
    return f"{n:,}".replace(",", ".")                     # locale with '.' thousands separators


class Log:
    """Builds a ProcMon CSV export."""
    HEADER = '"Time of Day","Process Name","PID","Operation","Path","Result","Detail"\n'

    def __init__(self):
        self.rows = []

    def row(self, op, path, result, detail=""):
        self.rows.append(f'"16:00:00,0000000","Dino2.exe","1234","{op}","{path}","{result}","{detail}"\n')

    def camera(self, dbs, offset):
        self.row("ReadFile", f"{MOD}\\Data\\{dbs}", "SUCCESS", f"Offset: {fmt_offset(offset)}, Length: 4.096, Priority: Normal")

    def probe(self, key, found=False):
        self.row("CreateFile", f"{MOD}\\Data\\{key}.dat", "SUCCESS" if found else "NAME NOT FOUND",
                 "Desired Access: Generic Read")

    def bytes(self):
        return ("\ufeff" + self.HEADER + "".join(self.rows)).encode("utf-8")


@unittest.skipUnless(os.path.isdir(GAME_DIR) and os.path.isfile(PREDICTED), "game data or research predictions missing")
class TestKeyCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PREDICTED, encoding="utf-8") as fh:
            pred = json.load(fh)
        cls.rooms = []                                     # (id, dbs, group, offset, k32, k16, a32, a16)
        same = None
        for dbs in ("ST000.DBS", "ST100.DBS", "ST101.DBS"):
            with open(os.path.join(GAME_DIR, "Data", dbs), "rb") as fh:
                groups = P.parse(fh.read(), "DBS")
            for gi, g in enumerate(groups):
                p = pred[f"{dbs}:{gi}"]
                entry = (f"{dbs}#{gi}", dbs, gi, g["offset"], p["crc32_pc53"], p["crc16_pc53"], p["crc32_pc24"], p["crc16_pc24"])
                if p["crc32_pc53"] != p["crc32_pc24"]:
                    cls.rooms.append(entry)
                elif same is None:
                    same = entry
        cls.same = same
        cls.rooms = cls.rooms[:8]

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="dc2_kc_")
        self.cat = Catalog(self.ws)
        for (item_id, dbs, gi, _, k32, k16, a32, a16) in self.rooms + [self.same]:
            self.cat.upsert_item({"id": item_id, "kind": "room", "label": item_id, "source_file": dbs, "grp": gi,
                                  "slot": 1, "stage": dbs[:5], "camera": gi, "content_sha1": item_id,
                                  "key32": k32, "key16": k16, "key32_alt": a32, "key16_alt": a16,
                                  "key_status": "predicted", "dup_of": None, "placeholder": 0,
                                  "original_png": "x.png", "thumb": "x.jpg"})
        self.cat.upsert_item({"id": "TITLE.DAT#5", "kind": "fullscreen", "label": "Title", "source_file": "TITLE.DAT",
                              "slot": 5, "content_sha1": "t", "key32": "ED666D62", "key16": "127F7F54",
                              "key_status": "computed", "dup_of": None, "placeholder": 0,
                              "original_png": "t.png", "thumb": "t.jpg"})

    def tearDown(self):
        self.cat.close()
        shutil.rmtree(self.ws)

    def run_log(self, log):
        events = keycheck.read_procmon_csv(log.bytes())
        return keycheck.analyze(self.cat, events, GAME_DIR)

    def visit(self, log, rooms, alt=False, noise=True):
        for (_, dbs, _, off, k32, _, a32, _) in rooms:
            log.camera(dbs, off)
            if noise:
                log.probe("DEADBEEF")                      # character/other texture lookups
            log.probe(a32 if alt else k32)

    def test_primary_confirmed_and_applied(self):
        log = Log()
        log.probe("ED666D62", found=True)
        self.visit(log, self.rooms[:6])
        r = self.run_log(log)
        self.assertEqual(r["decision"], "primary", r["message"])
        self.assertEqual(r["fullscreen_verified"], ["TITLE.DAT#5"])
        self.assertEqual(r["room_counts"]["primary"], 6)
        keycheck.apply(self.cat, r)
        seen, unseen = self.rooms[0][0], self.rooms[7][0]
        self.assertEqual(self.cat.get_item(seen)["key_status"], "verified")
        self.assertEqual(self.cat.get_item(unseen)["key_status"], "computed")
        self.assertEqual(self.cat.get_item(unseen)["key32"], self.rooms[7][4])
        self.assertEqual(self.cat.get_item("TITLE.DAT#5")["key_status"], "verified")

    def test_alternative_swaps_keys_once(self):
        log = Log()
        self.visit(log, self.rooms[:5], alt=True)
        r = self.run_log(log)
        self.assertEqual(r["decision"], "alt", r["message"])
        keycheck.apply(self.cat, r)
        keycheck.apply(self.cat, r)                       # applying twice must not swap back
        it = self.cat.get_item(self.rooms[7][0])
        self.assertEqual((it["key32"], it["key32_alt"], it["key16"]), (self.rooms[7][6], self.rooms[7][4], self.rooms[7][7]))
        self.assertEqual(it["key_status"], "computed")
        # a later extraction feeds fresh (prediction-order) data through adjust_keys
        fresh = {"id": it["id"], "kind": "room", "key32": self.rooms[7][4], "key16": self.rooms[7][5],
                 "key32_alt": self.rooms[7][6], "key16_alt": self.rooms[7][7], "key_status": "predicted"}
        out = keycheck.adjust_keys(fresh, self.cat.get_setting(keycheck.SETTING))
        self.assertEqual((out["key32"], out["key_status"]), (self.rooms[7][6], "computed"))

    def test_inconclusive_only_marks_seen(self):
        log = Log()
        self.visit(log, self.rooms[:2])
        log.camera(self.same[1], self.same[3])
        log.probe(self.same[4])
        r = self.run_log(log)
        self.assertEqual(r["decision"], "inconclusive")
        self.assertEqual(r["room_counts"], {"primary": 2, "alt": 0, "both": 1})
        keycheck.apply(self.cat, r)
        self.assertEqual(self.cat.get_item(self.rooms[0][0])["key_status"], "verified")
        self.assertEqual(self.cat.get_item(self.same[0])["key_status"], "verified")
        self.assertEqual(self.cat.get_item(self.rooms[5][0])["key_status"], "predicted")

    def test_mixed_and_contradicted(self):
        log = Log()
        self.visit(log, self.rooms[:3])
        self.visit(log, self.rooms[3:5], alt=True)
        self.assertEqual(self.run_log(log)["decision"], "mixed")
        log = Log()
        self.visit(log, self.rooms[:5])
        _, dbs, _, off, *_ = self.rooms[6]
        log.camera(dbs, off)
        log.probe("0BADC0DE")                              # the real key differs from both predictions
        r = self.run_log(log)
        self.assertEqual(r["decision"], "contradicted")
        self.assertEqual(r["contradicted"], {self.rooms[6][0]: ["0BADC0DE"]})
        log = Log()
        log.camera(dbs, off)
        log.probe("0BADC0DE")
        self.assertEqual(self.run_log(log)["decision"], "model_wrong")

    def test_bad_inputs(self):
        with self.assertRaises(keycheck.KeyCheckError):
            keycheck.read_procmon_csv(b"just,some,csv\n1,2,3\n")
        log = Log()
        log.camera(self.rooms[0][1], self.rooms[0][3])     # camera read but no .dat lookups at all
        with self.assertRaises(keycheck.KeyCheckError):
            self.run_log(log)

    def test_confirmed_room_keys_become_installable(self):
        rid = self.rooms[0][0]
        self.cat.db.execute("UPDATE items SET status='approved', approved_png='approved/x.png' WHERE id=?", (rid,))
        self.cat.db.commit()
        os.makedirs(os.path.join(self.ws, "approved"))
        with open(os.path.join(self.ws, "approved", "x.png"), "wb") as fh:
            fh.write(b"x")
        ok, blocked = Installer(self.cat, None, GAME_DIR).classify()
        self.assertEqual(ok, [])
        self.assertIn("key not confirmed", blocked[0][1])                 # still 'predicted'
        log = Log()
        self.visit(log, self.rooms[:5])
        keycheck.apply(self.cat, self.run_log(log))
        ok, blocked = Installer(self.cat, None, GAME_DIR).classify()
        self.assertEqual([it["id"] for it in ok], [rid])


@unittest.skipUnless(os.path.isdir(GAME_DIR), "game data missing")
class TestKeyCheckApi(unittest.TestCase):
    def test_import_and_apply(self):
        from test_m3 import AppBase                       # reuse the app fixture without running its tests here

        class T(AppBase):
            def runTest(self):
                pass
        t = T()
        t.setUp()
        try:
            cat = t.app.config["CTX"].cat
            cat.db.execute("UPDATE items SET key32='ED666D62', key16='127F7F54', key_status='computed', kind='fullscreen' WHERE id='A'")
            cat.db.commit()
            log = Log()
            log.probe("ED666D62")
            r = t.client.post("/api/keycheck/analyze", data={"file": (io.BytesIO(log.bytes()), "log.csv")},
                              content_type="multipart/form-data")
            self.assertEqual(r.status_code, 200, r.get_json())
            self.assertEqual(r.get_json()["fullscreen_verified"], ["A"])
            r = t.client.post("/api/keycheck/apply").get_json()
            self.assertEqual(r["counts"]["fullscreen"], {"verified": 1})
            self.assertEqual(t.client.post("/api/keycheck/apply").status_code, 400)   # report consumed
            r = t.client.post("/api/keycheck/analyze", data={"file": (io.BytesIO(b"nope"), "x.csv")},
                              content_type="multipart/form-data")
            self.assertEqual(r.status_code, 400)
        finally:
            t.tearDown()


if __name__ == "__main__":
    unittest.main()
