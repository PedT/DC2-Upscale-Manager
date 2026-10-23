r"""SQLite catalog: items (one per extracted background), upscale runs, candidates, settings.

Item status flow:
    todo -> queued -> upscaling -> review -> approved
                                        \-> todo   (rejected; the model is added to models_tried)
    queued/upscaling -> todo   (run cancelled or failed)
    approved -> todo           (reopened)

key_status:
    verified  - replacement shown working in the game
    computed  - exact offline formula (full-screen images; cross-checked against Rex-HD names)
    predicted - decoder model only (rooms); install stays blocked until confirmed
    unknown   - no key available
"""
import json
import os
import sqlite3
import threading
import time

STATUSES = ("todo", "queued", "upscaling", "review", "approved")
TRANSITIONS = {
    "todo": {"queued"},
    "queued": {"upscaling", "todo"},
    "upscaling": {"review", "todo"},
    "review": {"approved", "todo"},
    "approved": {"todo"},
}
KEY_STATUSES = ("verified", "computed", "predicted", "unknown")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,              -- fullscreen | room
    label         TEXT NOT NULL,
    source_file   TEXT NOT NULL,
    grp           INTEGER,                    -- DBS group (= camera) index
    slot          INTEGER,                    -- container slot holding the image
    stage         TEXT,
    camera        INTEGER,
    content_sha1  TEXT NOT NULL,              -- of the source payload (raw image / JPEG)
    key32         TEXT,
    key16         TEXT,
    key32_alt     TEXT,
    key16_alt     TEXT,
    key_status    TEXT NOT NULL,
    dup_of        TEXT,                       -- id of the item with identical content, if any
    placeholder   INTEGER NOT NULL DEFAULT 0, -- blank filler image
    original_png  TEXT NOT NULL,              -- relative to the workspace
    thumb         TEXT NOT NULL,
    approved_png  TEXT,
    status        TEXT NOT NULL DEFAULT 'todo',
    models_tried  TEXT NOT NULL DEFAULT '[]',
    installed     INTEGER NOT NULL DEFAULT 0,
    notes         TEXT,
    created       REAL NOT NULL,
    updated       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
CREATE TABLE IF NOT EXISTS runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    model     TEXT NOT NULL,
    scale     INTEGER NOT NULL,
    started   REAL,
    finished  REAL,
    state     TEXT NOT NULL,                  -- queued | running | done | failed | cancelled | interrupted
    log       TEXT,
    params    TEXT,                           -- JSON: gpu, threads, workers, run folder
    total     INTEGER NOT NULL DEFAULT 0,
    done      INTEGER NOT NULL DEFAULT 0,
    failed    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS candidates (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   TEXT NOT NULL REFERENCES items(id),
    run_id    INTEGER NOT NULL REFERENCES runs(id),
    model     TEXT NOT NULL,
    path      TEXT NOT NULL,
    created   REAL NOT NULL,
    verdict   TEXT NOT NULL DEFAULT 'pending' -- pending | approved | rejected
);
"""

# Columns refreshed from the game data on every extraction (status/workflow columns are kept).
SOURCE_COLUMNS = ("kind", "label", "source_file", "grp", "slot", "stage", "camera", "content_sha1",
                  "key32", "key16", "key32_alt", "key16_alt", "key_status", "dup_of", "placeholder",
                  "original_png", "thumb")


# Columns added after the first release; added to existing databases on open.
MIGRATIONS = {
    "runs": [("params", "TEXT"), ("total", "INTEGER NOT NULL DEFAULT 0"),
             ("done", "INTEGER NOT NULL DEFAULT 0"), ("failed", "INTEGER NOT NULL DEFAULT 0")],
}
RUN_FIELDS = ("state", "started", "finished", "log", "params", "total", "done", "failed")


class TransitionError(ValueError):
    pass


class Catalog:
    FILENAME = "catalog.sqlite"

    def __init__(self, workspace):
        self.workspace = workspace
        os.makedirs(workspace, exist_ok=True)
        self.path = os.path.join(workspace, self.FILENAME)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self):
        with self._lock:
            self.db.close()

    def _all(self, sql, args=()):
        """Locked read: the web server reads from several threads while a job writes."""
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def _one(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchone()

    def version(self):
        """Changes whenever any item, run or candidate changes (cheap polling check for the UI)."""
        row = self._one("SELECT (SELECT IFNULL(MAX(updated), 0) FROM items) AS i, "
                        "(SELECT COUNT(*) FROM runs) AS r, (SELECT COUNT(*) FROM candidates) AS c, "
                        "(SELECT IFNULL(SUM(done + failed), 0) FROM runs) AS d")
        return f"{row['i']:.6f}-{row['r']}-{row['c']}-{row['d']}"

    # ---- settings -------------------------------------------------------------------------
    def get_setting(self, key, default=None):
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self._lock:
            self.db.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
            self.db.commit()

    # ---- items ----------------------------------------------------------------------------
    def upsert_item(self, item):
        """Insert or refresh an item from extraction. Returns 'new', 'unchanged' or 'changed'.
        A changed source (different content_sha1) resets the workflow state to 'todo'."""
        now = time.time()
        with self._lock:
            old = self.db.execute("SELECT content_sha1 FROM items WHERE id=?", (item["id"],)).fetchone()
            if old is None:
                cols = ("id",) + SOURCE_COLUMNS + ("created", "updated")
                vals = [item["id"]] + [item.get(c) for c in SOURCE_COLUMNS] + [now, now]
                self.db.execute(f"INSERT INTO items({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
                result = "new"
            else:
                sets = ",".join(f"{c}=?" for c in SOURCE_COLUMNS)
                vals = [item.get(c) for c in SOURCE_COLUMNS]
                result = "unchanged" if old["content_sha1"] == item["content_sha1"] else "changed"
                if result == "changed":
                    sets += ",status='todo',approved_png=NULL,installed=0,notes='source image changed'"
                self.db.execute(f"UPDATE items SET {sets},updated=? WHERE id=?", vals + [now, item["id"]])
            self.db.commit()
        return result

    def get_item(self, item_id):
        row = self._one("SELECT * FROM items WHERE id=?", (item_id,))
        return _item(row) if row else None

    def items(self, kind=None, status=None, include_dups=True, include_placeholders=True):
        sql, args = "SELECT * FROM items WHERE 1=1", []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if status:
            sql += " AND status=?"
            args.append(status)
        if not include_dups:
            sql += " AND dup_of IS NULL"
        if not include_placeholders:
            sql += " AND placeholder=0"
        sql += " ORDER BY kind, source_file, grp, slot"
        return [_item(r) for r in self._all(sql, args)]

    def counts(self):
        out = {}
        for r in self._all("SELECT kind, status, COUNT(*) n FROM items GROUP BY kind, status"):
            out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        return out

    def set_status(self, item_id, new, model=None, approved_png=None):
        """Move an item along the workflow; raises TransitionError for illegal moves.
        Passing model when going review -> todo records it in models_tried (a rejection)."""
        if new not in STATUSES:
            raise TransitionError(f"unknown status {new!r}")
        with self._lock:
            row = self.db.execute("SELECT status, models_tried FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            cur = row["status"]
            if new not in TRANSITIONS[cur]:
                raise TransitionError(f"{item_id}: {cur} -> {new} is not allowed")
            tried = json.loads(row["models_tried"])
            if model and model not in tried:
                tried.append(model)
            fields = {"status": new, "models_tried": json.dumps(tried), "updated": time.time()}
            if new == "approved":
                fields["approved_png"] = approved_png
            elif cur == "approved":
                fields["approved_png"] = None
                fields["installed"] = 0
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE items SET {sets} WHERE id=?", list(fields.values()) + [item_id])
            self.db.commit()

    def add_note(self, item_id, note):
        with self._lock:
            self.db.execute("UPDATE items SET notes=?, updated=? WHERE id=?", (note, time.time(), item_id))
            self.db.commit()

    # ---- runs -----------------------------------------------------------------------------
    def create_run(self, model, scale, params, total):
        with self._lock:
            cur = self.db.execute("INSERT INTO runs(model, scale, state, params, total) VALUES(?,?,?,?,?)",
                                  (model, scale, "queued", json.dumps(params), total))
            self.db.commit()
            return cur.lastrowid

    def update_run(self, run_id, **fields):
        bad = set(fields) - set(RUN_FIELDS)
        if bad:
            raise ValueError(f"unknown run fields {bad}")
        if "params" in fields:
            fields["params"] = json.dumps(fields["params"])
        with self._lock:
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE runs SET {sets} WHERE id=?", list(fields.values()) + [run_id])
            self.db.commit()

    def get_run(self, run_id):
        row = self._one("SELECT * FROM runs WHERE id=?", (run_id,))
        return _run(row) if row else None

    def runs(self, state=None):
        sql, args = "SELECT * FROM runs", []
        if state:
            sql += " WHERE state=?"
            args.append(state)
        return [_run(r) for r in self._all(sql + " ORDER BY id", args)]

    # ---- candidates -----------------------------------------------------------------------
    def add_candidate(self, item_id, run_id, model, path):
        with self._lock:
            cur = self.db.execute("INSERT INTO candidates(item_id, run_id, model, path, created) "
                                  "VALUES(?,?,?,?,?)", (item_id, run_id, model, path, time.time()))
            self.db.commit()
            return cur.lastrowid

    def get_candidate(self, cand_id):
        row = self._one("SELECT * FROM candidates WHERE id=?", (cand_id,))
        return dict(row) if row else None

    def set_verdict(self, cand_id, verdict):
        if verdict not in ("pending", "approved", "rejected"):
            raise ValueError(verdict)
        with self._lock:
            self.db.execute("UPDATE candidates SET verdict=? WHERE id=?", (verdict, cand_id))
            self.db.commit()

    def set_approved_png(self, item_id, rel_path):
        """Swap the image of an already-approved item (e.g. a hand-edited version)."""
        with self._lock:
            n = self.db.execute("UPDATE items SET approved_png=?, installed=0, updated=? WHERE id=? AND status='approved'",
                                (rel_path, time.time(), item_id)).rowcount
            self.db.commit()
        if not n:
            raise TransitionError(f"{item_id} is not approved")

    def update_keys(self, item_id, **fields):
        allowed = {"key32", "key16", "key32_alt", "key16_alt", "key_status"}
        if set(fields) - allowed:
            raise ValueError(f"not key fields: {set(fields) - allowed}")
        with self._lock:
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE items SET {sets}, updated=? WHERE id=?",
                            list(fields.values()) + [time.time(), item_id])
            self.db.commit()

    def set_installed(self, item_id, installed):
        with self._lock:
            self.db.execute("UPDATE items SET installed=?, updated=? WHERE id=?",
                            (int(bool(installed)), time.time(), item_id))
            self.db.commit()

    def candidates(self, item_id=None, run_id=None, verdict=None):
        sql, args = "SELECT * FROM candidates WHERE 1=1", []
        for col, val in (("item_id", item_id), ("run_id", run_id), ("verdict", verdict)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        return [dict(r) for r in self._all(sql + " ORDER BY id", args)]


def _run(row):
    d = dict(row)
    d["params"] = json.loads(d["params"]) if d.get("params") else {}
    return d


def _item(row):
    d = dict(row)
    d["models_tried"] = json.loads(d["models_tried"])
    d["placeholder"] = bool(d["placeholder"])
    d["installed"] = bool(d["installed"])
    return d
