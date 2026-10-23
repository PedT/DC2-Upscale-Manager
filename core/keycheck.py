"""Confirm replacement keys from a Process Monitor log of the game running (phase 2, step 1).

While the game runs, GOG's ddraw.dll checks every uploaded texture for a replacement file:
    CreateFile  <mod>\\Data\\XXXXXXXX.dat   -> NAME NOT FOUND (no replacement) / SUCCESS (installed)
and the game reads each camera's background from its stage file:
    ReadFile    <mod>\\Data\\STxxx.DBS   Offset: <group offset>
So the log tells us which keys the game really looks for, and which camera was being loaded.

analyze()  matches probed keys against every item's key32 / key16 / key32_alt / key16_alt.
  Rooms: a match on key32 supports the primary decoder model (x87 53-bit precision), a match on
  key32_alt the alternative (24-bit). Rooms where both models agree prove nothing about the choice.
  A camera whose DBS read is followed by probes that match neither key contradicts the model.
apply()    marks matched items 'verified'. If the evidence is consistent (one model, no
  contradictions, enough discriminating rooms) every other room becomes 'computed' (model
  confirmed); if the alternative model won, every room's key32/key16 and *_alt are swapped first.
  The outcome is stored in the catalog settings so a later re-extraction keeps it (adjust_keys()).
"""
import collections
import csv
import io
import os
import re

from .dc2 import dc2pack as P

PROBE_RE = re.compile(r"\\data\\([0-9a-f]{8})\.dat$", re.I)
DBS_RE = re.compile(r"\\data\\(st[0-9a-z]{3}\.dbs)$", re.I)
OFFSET_RE = re.compile(r"Offset:\s*([0-9][0-9.,\s  ]*)", re.I)
MIN_DISCRIMINATING = 5              # rooms where the two models differ, needed to confirm a model
SETTING = "key_check"


class KeyCheckError(ValueError):
    pass


# ---- reading the log ------------------------------------------------------------------------------
def read_procmon_csv(data):
    """ProcMon 'Save... CSV' export (bytes) -> list of event dicts in log order."""
    text = None
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = data.decode(enc)
            if "Operation" in text[:2000]:
                break
        except UnicodeDecodeError:
            continue
    if not text or "Operation" not in text[:2000] or "Path" not in text[:2000]:
        raise KeyCheckError("this does not look like a Process Monitor CSV export (no Operation/Path columns)")
    rows = csv.DictReader(io.StringIO(text))
    events = []
    for n, r in enumerate(rows):
        events.append({"n": n, "time": r.get("Time of Day", ""), "process": r.get("Process Name", ""),
                       "op": r.get("Operation", ""), "path": r.get("Path", ""), "result": r.get("Result", ""),
                       "detail": r.get("Detail", "")})
    return events


def _offset(detail):
    m = OFFSET_RE.search(detail or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def _group_offsets(game_dir, dbs_name, cache):
    """{byte offset of camera group -> group index} for one DBS file (read-only)."""
    key = dbs_name.upper()
    if key not in cache:
        path = os.path.join(game_dir, "Data", key)
        offsets = {}
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                for gi, g in enumerate(P.parse(fh.read(), "DBS")):
                    offsets[g["offset"]] = gi
        cache[key] = offsets
    return cache[key]


# ---- analysis -------------------------------------------------------------------------------------
def analyze(cat, events, game_dir):
    items = cat.items()
    index = collections.defaultdict(list)                  # key -> [(item_id, field)]
    by_id = {it["id"]: it for it in items}
    for it in items:
        for field in ("key32", "key16", "key32_alt", "key16_alt"):
            if it.get(field):
                index[it[field].upper()].append((it["id"], field))

    probes, found = [], 0
    timeline = []                                           # ("cam", item_id) / ("probe", key)
    offsets_cache = {}
    unknown_reads = 0
    for e in events:
        path = e["path"]
        m = PROBE_RE.search(path)
        if m and e["op"] == "CreateFile":
            key = m.group(1).upper()
            probes.append(key)
            found += e["result"] == "SUCCESS"
            timeline.append(("probe", key))
            continue
        m = DBS_RE.search(path)
        if m and e["op"] == "ReadFile":
            off = _offset(e["detail"])
            gi = _group_offsets(game_dir, m.group(1), offsets_cache).get(off) if off is not None else None
            if gi is None:
                unknown_reads += 1
                continue
            timeline.append(("cam", f"{m.group(1).upper()}#{gi}"))

    if not probes:
        raise KeyCheckError("the log contains no Data\\XXXXXXXX.dat lookups - check the ProcMon filter "
                            "(Process Name is Dino2.exe, Path contains \\Data\\) and that the game ran from the install target")

    matched = {}                                            # item_id -> set(fields)
    for key in set(probes):
        for item_id, field in index.get(key, []):
            matched.setdefault(item_id, set()).add(field)

    rooms = {}
    for item_id, fields in matched.items():
        it = by_id[item_id]
        if it["kind"] != "room":
            continue
        same = it["key32"] == it["key32_alt"]
        if same:
            verdict = "both"
        elif "key32" in fields or "key16" in fields:
            verdict = "primary"
        else:
            verdict = "alt"
        rooms[item_id] = verdict

    # Cameras loaded (DBS read) whose following probes match neither prediction.
    contradicted = {}
    for i, (kind, val) in enumerate(timeline):
        if kind != "cam" or val in rooms:
            continue
        it = by_id.get(val)
        if it is None or it["dup_of"]:
            continue
        window = []
        for kind2, val2 in timeline[i + 1:]:
            if kind2 == "cam":
                break
            window.append(val2)
        if window:
            contradicted[val] = sorted(set(window) - set(index))[:20]

    fullscreen = sorted(i for i in matched if by_id[i]["kind"] == "fullscreen")
    counts = collections.Counter(rooms.values())
    discriminating = counts["primary"] + counts["alt"]
    if contradicted and not discriminating:
        decision = "model_wrong"
    elif counts["primary"] and counts["alt"]:
        decision = "mixed"
    elif contradicted:
        decision = "contradicted"
    elif discriminating >= MIN_DISCRIMINATING:
        decision = "primary" if counts["primary"] else "alt"
    else:
        decision = "inconclusive"
    return {
        "events": len(events), "probes": len(probes), "unique_probes": len(set(probes)), "found_installed": found,
        "dbs_reads_unmapped": unknown_reads,
        "fullscreen_verified": fullscreen,
        "rooms": rooms, "room_counts": {"primary": counts["primary"], "alt": counts["alt"], "both": counts["both"]},
        "contradicted": contradicted, "decision": decision, "min_discriminating": MIN_DISCRIMINATING,
        "message": _message(decision, counts, discriminating, contradicted),
    }


def _message(decision, counts, discriminating, contradicted):
    p, a, b = counts["primary"], counts["alt"], counts["both"]
    return {
        "primary": f"Confirmed: {p} room(s) matched the primary prediction, none the alternative, no contradictions. "
                   f"All room keys can be marked confirmed.",
        "alt": f"Confirmed: {a} room(s) matched the ALTERNATIVE prediction, none the primary, no contradictions. "
               f"Applying swaps every room to the alternative keys and marks them confirmed.",
        "inconclusive": f"Not enough evidence yet: {discriminating} room(s) where the two predictions differ were seen "
                        f"(need {MIN_DISCRIMINATING}); {b} matched where both predictions agree. Visit more rooms and "
                        f"import a longer log. Matched items can still be marked verified.",
        "mixed": f"Inconsistent: {p} room(s) matched the primary prediction and {a} the alternative. The decoder model "
                 f"is not reliable; only the rooms actually seen can be trusted (dumper hook needed for the rest).",
        "contradicted": f"{len(contradicted)} camera(s) were loaded but none of their probes matched a prediction, "
                        f"although {p + a + b} other room(s) did. Only the rooms actually seen can be trusted.",
        "model_wrong": f"The predictions do not match: {len(contradicted)} camera(s) were loaded and none of the probed "
                       f"keys matched. Room keys must be collected with a dumper hook instead.",
    }[decision]


# ---- applying -------------------------------------------------------------------------------------
def apply(cat, report):
    """Store the outcome and update the catalog. Returns counts of changes."""
    decision = report["decision"]
    state = cat.get_setting(SETTING, {}) or {}
    currently_alt = state.get("variant") == "alt"           # catalog keys already swapped by an earlier apply
    verified = set(state.get("verified_ids", []))
    verified.update(report["fullscreen_verified"])
    verified.update(report["rooms"])
    variant = state.get("variant")
    if decision in ("primary", "alt"):
        variant = decision
    state.update({"variant": variant, "verified_ids": sorted(verified), "last_decision": decision,
                  "last_message": report["message"]})
    cat.set_setting(SETTING, state)
    changed = 0
    for it in cat.items():
        new = adjust_keys(dict(it), state, currently_alt)
        fields = {k: new[k] for k in ("key32", "key16", "key32_alt", "key16_alt", "key_status") if new[k] != it[k]}
        if fields:
            cat.update_keys(it["id"], **fields)
            changed += 1
    return {"changed": changed, "verified": len(verified), "variant": variant}


def adjust_keys(item, state, currently_alt=False):
    """Apply a stored key-check outcome to an item dict. Fresh extraction data is in prediction order
    (currently_alt=False); catalog rows already adjusted for the alternative model pass True."""
    if not state:
        return item
    if item["kind"] == "room" and item.get("key32"):
        if (state.get("variant") == "alt") != currently_alt:
            item["key32"], item["key32_alt"] = item["key32_alt"], item["key32"]
            item["key16"], item["key16_alt"] = item["key16_alt"], item["key16"]
        if state.get("variant") in ("primary", "alt") and item["key_status"] == "predicted":
            item["key_status"] = "computed"
    if item["id"] in set(state.get("verified_ids", [])) and item.get("key32"):
        item["key_status"] = "verified"
    return item
