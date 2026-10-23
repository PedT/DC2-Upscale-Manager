"""dc2pack - Dino Crisis 2 (PC) DAT/DBS/TEX container lister / extractor / rebuilder.

Container layout (verified on all 372 .DAT, 89 .DBS and 28 .TEX files of the GOG build):
  group := table[0x800] + payloads
  table := up to 64 entries of 32 bytes {u32 type, u32 size, u32 p1, u32 p2, 16 zero bytes};
           unused entries = b"dummy header    " + 16 zero bytes
  payloads follow the table in table order, each starting on a 0x800 boundary
  (zero padded). A file is one group (.DAT/.TEX) or several consecutive groups
  (.DBS: one group per camera; the game seeks to them via a camera table that
  lives in the stage's ST???.DAT, see `camtable`).

Commands
  python -I dc2pack.py list <file>
  python -I dc2pack.py extract <file> <outdir> [--png] [--no-decode]
  python -I dc2pack.py rebuild <manifest.json> <out_file> [--recompress changed|all|none]
                                                     [--mode optimal|lazy|greedy|literal]
  python -I dc2pack.py verify <file>                 (strict-decode every LZ chunk)
  python -I dc2pack.py camtable <ST???.DAT> <ST???.DBS>  (locate the DBS camera table)
  python -I dc2pack.py replace <in> <out> --slot N [--group G] (--png img.png | --raw file)
                                  [--mode optimal] [--allow-layout-change]
        e.g. new title background:  replace TITLE.DAT out/TITLE.DAT --slot 5 --png bg320x240.png
        e.g. new room JPEG:         replace ST000.DBS out/ST000.DBS --group 1 --slot 1 --raw new.jpg
  python -I dc2pack.py camsync <orig ST.DAT> <orig ST.DBS> <new ST.DBS> <out ST.DAT>
        (rewrite the stage camera table after a DBS group layout change)
  python -I dc2pack.py roombg <ST.DBS> <group> <new320x240.png> <out.DBS> [--quality 90]
        (new room background: JPEG for slot 1 + occlusion cells of slot 3 regenerated)
  Output paths inside C:/GOG Games are refused.

`rebuild` never touches the source file; it reads the payload files named in the
manifest. --recompress:
  changed (default) - LZ entries whose decoded file differs from the original
                      (sha1 in manifest) are recompressed, all others reuse the
                      stored compressed payload -> byte-identical if nothing changed
  all               - recompress every LZ entry with our encoder
  none              - always use the stored payload files
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dc2codec as C  # noqa: E402

SECTOR = 0x800
ENTRY = 32
MAX_ENTRIES = SECTOR // ENTRY        # 64
DUMMY_ENTRY = b"dummy header    " + b"\0" * 16

TYPE_MEANING = {
    0: "raw -> memcpy to PC VA p1 (handler 0x4044c0)",
    1: "raw 32x32-halfword tiles -> VRAM rect p1/p2 (handler 0x4044f0)",
    2: "raw -> VRAM rect p1/p2, e.g. CLUT (handler 0x404680)",
    3: "sound bank -> slot p1 (handler 0x4046c0)",
    4: "audio blob -> slot p1, param p2 (handler 0x404770)",
    5: "LZSS -> memory VA p1 (handler 0x4047f0)",
    6: "LZSS -> 0x5a0000 -> 32x32 tiles -> VRAM rect p1/p2 (handler 0x404810)",
    7: "one-shot global blob (handler 0x4048f0)",
}
DBS_SLOT_MEANING = {
    0: "DBS slot0: memcpy to 0x6b9500 (hard-coded), <=0x800 bytes; occlusion tile list "
       "u16 count,u16 0,{u8 tx,u8 ty,u16 z}*count (16px tiles) [inferred]",
    1: "DBS slot1: baseline JPEG 320x240, decoded by libjpeg (0x4d2990) into 0x6c1500 = room background",
    2: "DBS slot2: raw -> VRAM rect p1/p2 (CLUT), <=0x800 bytes; type 'dumm' ends the group",
    3: "DBS slot3: LZSS always; type 0 -> malloc(p1) RAM buffer (occlusion-tile pixels), "
       "type!=0 -> 0x5a0000 then one VRAM upload (8bpp overlay texture page)",
}


def align(n: int) -> int:
    return (n + SECTOR - 1) & ~(SECTOR - 1)


def kind_of(name: str) -> str:
    ext = os.path.splitext(name)[1].upper()
    return {".DBS": "DBS", ".TEX": "TEX"}.get(ext, "DAT")


def entry_is_lz(kind: str, slot: int, ctype: int) -> bool:
    if kind == "DBS":
        return slot == 3
    return C.is_lz_type(ctype)


# ----------------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------------
class ContainerError(ValueError):
    pass


def parse(data: bytes, kind: str = "DAT"):
    """Return list of groups: {"offset", "length", "entries": [...]}.
    Each entry: {"slot","type","size","p1","p2","reserved","offset"}."""
    groups = []
    g = 0
    n = len(data)
    while g < n:
        if g + SECTOR > n:
            raise ContainerError(f"truncated table at 0x{g:x}")
        ents = []
        for k in range(MAX_ENTRIES):
            raw = data[g + k * ENTRY: g + (k + 1) * ENTRY]
            if raw[:16] == DUMMY_ENTRY[:16]:
                if raw != DUMMY_ENTRY:
                    raise ContainerError(f"odd dummy entry at 0x{g + k * ENTRY:x}")
                # all following entries must be dummies too
                rest = data[g + k * ENTRY: g + SECTOR]
                if rest != DUMMY_ENTRY * (MAX_ENTRIES - k):
                    raise ContainerError(f"non-dummy data after dummy at 0x{g:x}")
                break
            t, sz, p1, p2 = struct.unpack_from("<IIII", raw)
            if t > 15 or sz > n:
                raise ContainerError(f"implausible entry {k} at 0x{g:x}: type {t} size {sz}")
            ents.append({"slot": k, "type": t, "size": sz, "p1": p1, "p2": p2,
                         "reserved": raw[16:].hex()})
        if not ents:
            raise ContainerError(f"empty table at 0x{g:x}")
        off = g + SECTOR
        for e in ents:
            e["offset"] = off
            if off + e["size"] > n:
                raise ContainerError(f"payload of slot {e['slot']} runs past EOF")
            pad = data[off + e["size"]: off + align(e["size"])]
            e["pad_zero"] = not any(pad)
            off += align(e["size"])
        groups.append({"offset": g, "length": off - g, "entries": ents})
        g = off
        if kind != "DBS":
            break
    end = groups[-1]["offset"] + groups[-1]["length"]
    if end != n:
        raise ContainerError(f"container covers 0x{end:x} of 0x{n:x} bytes "
                             f"(the generic loader needs an exact match)")
    return groups


def meaning(kind: str, e: dict) -> str:
    if kind == "DBS":
        return DBS_SLOT_MEANING.get(e["slot"], "DBS slot >3 (ignored by the camera loader)") + f" [type {e['type']}]"
    return TYPE_MEANING.get(e["type"], "type > 7: skipped by the loader")


def describe(kind: str, e: dict) -> str:
    t = e["type"]
    s = f"type={t} size={e['size']:<8d} p1=0x{e['p1']:08x} p2=0x{e['p2']:08x} @0x{e['offset']:x}"
    vram = (t in (1, 2, 6, 8)) and not (kind == "DBS" and e["slot"] in (0, 1))
    if kind == "DBS" and e["slot"] == 3 and t == 0:
        vram = False
    if vram:
        x, y, w, h = C.vram_rect(e["p1"], e["p2"])
        s += f"  VRAM x={x} y={y} w={w} h={h}"
    elif t in (0, 5) and not (kind == "DBS"):
        s += f"  -> VA 0x{e['p1']:x}"
    if entry_is_lz(kind, e["slot"], t):
        s += "  [LZSS]"
    return s


# ----------------------------------------------------------------------------
# build
# ----------------------------------------------------------------------------
def build_group(entries):
    """entries: list of dicts with type,p1,p2,payload(bytes)[,reserved hex]. Returns bytes."""
    if len(entries) > MAX_ENTRIES:
        raise ContainerError("more than 64 entries in one table")
    table = bytearray()
    body = bytearray()
    for e in entries:
        payload = e["payload"]
        res = bytes.fromhex(e.get("reserved", "")) if e.get("reserved") else b"\0" * 16
        if len(res) != 16:
            raise ContainerError("reserved must be 16 bytes")
        table += struct.pack("<IIII", e["type"], len(payload), e["p1"], e["p2"]) + res
        body += payload + b"\0" * (align(len(payload)) - len(payload))
    table += DUMMY_ENTRY * (MAX_ENTRIES - len(entries))
    assert len(table) == SECTOR
    return bytes(table) + bytes(body)


def sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


# ----------------------------------------------------------------------------
# extract / rebuild
# ----------------------------------------------------------------------------
def extract(path: str, outdir: str, decode: bool = True, png: bool = False) -> dict:
    _safe_out(outdir)
    data = open(path, "rb").read()
    name = os.path.basename(path)
    kind = kind_of(name)
    groups = parse(data, kind)
    os.makedirs(outdir, exist_ok=True)
    man = {"format": "dc2-container/1", "source": name, "source_size": len(data),
           "source_sha1": sha1(data), "kind": kind, "groups": []}
    for gi, g in enumerate(groups):
        mg = {"index": gi, "offset": g["offset"], "offset_hex": f"0x{g['offset']:x}",
              "sector": g["offset"] // SECTOR, "length": g["length"], "entries": []}
        for e in g["entries"]:
            slot, t = e["slot"], e["type"]
            base = f"g{gi:02d}_e{slot:02d}_t{t}"
            payload = data[e["offset"]: e["offset"] + e["size"]]
            lz = entry_is_lz(kind, slot, t)
            ext = ".bin"
            if kind == "DBS" and slot == 1 and payload[:2] == b"\xff\xd8":
                ext = ".jpg"
            fn = base + (".lz" if lz else ext)
            open(os.path.join(outdir, fn), "wb").write(payload)
            me = {"slot": slot, "type": t, "size": e["size"], "p1": e["p1"], "p2": e["p2"],
                  "p1_hex": f"0x{e['p1']:08x}", "p2_hex": f"0x{e['p2']:08x}",
                  "payload_offset": e["offset"], "payload_offset_hex": f"0x{e['offset']:x}",
                  "file": fn, "sha1": sha1(payload), "lz": lz, "meaning": meaning(kind, e)}
            if e["reserved"] != "00" * 16:
                me["reserved"] = e["reserved"]
            if t in (1, 2, 6, 8) or (kind == "DBS" and slot == 2):
                x, y, w, h = C.vram_rect(e["p1"], e["p2"])
                me["vram"] = {"x": x, "y": y, "w": w, "h": h}
            if lz and decode:
                raw = C.lz_decompress(payload)
                dfn = base + ".dec.bin"
                open(os.path.join(outdir, dfn), "wb").write(raw)
                me["decoded_file"] = dfn
                me["decoded_size"] = len(raw)
                me["decoded_sha1"] = sha1(raw)
                if png:
                    try:
                        _preview(kind, me, raw, outdir, base)
                    except Exception as ex:  # previews are best effort
                        me["preview_error"] = str(ex)
            mg["entries"].append(me)
        man["groups"].append(mg)
    with open(os.path.join(outdir, "manifest.json"), "w") as fh:
        json.dump(man, fh, indent=1)
    return man


def _preview(kind, me, raw, outdir, base):
    t = me["type"]
    if t == 6 and "vram" in me:
        v = me["vram"]
        lin = C.detile32(raw, v["w"], v["h"])
        C.save_png16(lin, v["w"], v["h"], os.path.join(outdir, base + ".vram16.png"))
        me["preview"] = base + ".vram16.png (16-bit view of the VRAM rect; indexed textures look garbled)"
    elif kind == "DBS" and me["slot"] == 3 and t != 0 and "vram" in me:
        v = me["vram"]
        C.save_png16(raw, v["w"], v["h"], os.path.join(outdir, base + ".vram16.png"))
        me["preview"] = base + ".vram16.png"
    elif len(raw) == 320 * 240 * 2:
        C.save_png16(raw, 320, 240, os.path.join(outdir, base + ".320x240.png"))
        me["preview"] = base + ".320x240.png (guess: 320x240 BGR555 picture)"


def _check_decoded_size(kind, slot, ctype, p1, p2, n, log=print):
    """Return the (possibly updated) p1 for a re-encoded LZ entry and warn about
    decoded sizes the consumer will not expect."""
    if kind == "DBS" and slot == 3 and ctype == 0:
        if n != p1:
            log(f"  slot 3: decoded size {n} != malloc size p1 {p1} -> p1 updated to {n}")
        return n
    if ctype == 6:
        x, y, w, h = C.vram_rect(p1, p2)
        if n != C.type6_size(w, h):
            log(f"  WARNING: type-6 decoded size {n} != tile grid {C.type6_size(w, h)} for {w}x{h}")
    elif kind == "DBS" and slot == 3:
        x, y, w, h = C.vram_rect(p1, p2)
        if n != w * h * 2:
            log(f"  WARNING: DBS slot-3 decoded size {n} != {w}x{h}x2")
    return p1


def rebuild(manifest_path: str, out_path: str, recompress: str = "changed",
            mode: str = "optimal", log=print) -> bytes:
    if out_path:
        _safe_out(out_path)
    mdir = os.path.dirname(os.path.abspath(manifest_path))
    man = json.load(open(manifest_path))
    kind = man["kind"]
    out = bytearray()
    new_layout = []
    for mg in man["groups"]:
        ents = []
        for me in mg["entries"]:
            stored = open(os.path.join(mdir, me["file"]), "rb").read()
            payload = stored
            if me.get("lz"):
                dfile = me.get("decoded_file")
                raw = open(os.path.join(mdir, dfile), "rb").read() if dfile and os.path.exists(os.path.join(mdir, dfile)) else None
                do = False
                if recompress == "all" and raw is not None:
                    do = True
                elif recompress == "changed" and raw is not None and sha1(raw) != me.get("decoded_sha1"):
                    do = True
                if do:
                    payload = C.lz_compress(raw, mode)
                    if C.lz_decompress(payload, len(raw)) != raw:
                        raise ContainerError("encoder self-check failed")
                    log(f"  g{mg['index']:02d} slot {me['slot']}: recompressed {len(raw)} -> {len(payload)} "
                        f"(orig {me['size']})")
            p1 = me["p1"]
            if me.get("lz") and payload is not stored:
                p1 = _check_decoded_size(kind, me["slot"], me["type"], me["p1"], me["p2"], len(raw), log)
            ents.append({"type": me["type"], "p1": p1, "p2": me["p2"],
                         "reserved": me.get("reserved", ""), "payload": payload})
        # DBS camera loader constraints (hard-coded 0x800 strides)
        if kind == "DBS":
            if len(ents) >= 1 and len(ents[0]["payload"]) > SECTOR:
                raise ContainerError("DBS slot0 must be <= 0x800 bytes (loader assumes slot1 at +0x1000)")
            if len(ents) >= 3 and len(ents[2]["payload"]) > SECTOR:
                raise ContainerError("DBS slot2 must be <= 0x800 bytes (loader assumes slot3 = slot2 + 0x800)")
        gb = build_group(ents)
        new_layout.append((len(out) // SECTOR, len(gb), mg.get("sector"), mg.get("length")))
        out += gb
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(out)
    if kind == "DBS":
        changed = [(i, a, b, c, d) for i, (a, b, c, d) in enumerate(new_layout) if (a, b) != (c, d)]
        if changed:
            log("  WARNING: DBS group layout changed -> the camera table in the stage ST???.DAT "
                "(type-5 chunk) must be updated:")
            for i, a, b, c, d in changed:
                log(f"    group {i}: sector {c}->{a}, length 0x{d:x}->0x{b:x}")
    return bytes(out)


# ----------------------------------------------------------------------------
# DBS camera table (stored inside the stage ST???.DAT, LZSS type-5 chunk)
# ----------------------------------------------------------------------------
def find_camtable(dat_path: str, dbs_path: str):
    """Locate the (sector, length) camera table that the DBS loader (0x403ac0)
    reads through G+0xa6c. Returns list of hits: (entry_slot, chunk_va, offset_in_decoded, pairs)."""
    dbs = open(dbs_path, "rb").read()
    groups = parse(dbs, "DBS")
    pairs = [(g["offset"] // SECTOR, g["length"]) for g in groups]
    pat = b"".join(struct.pack("<II", a, b) for a, b in pairs)
    dat = open(dat_path, "rb").read()
    hits = []
    for g in parse(dat, kind_of(dat_path)):
        for e in g["entries"]:
            payload = dat[e["offset"]: e["offset"] + e["size"]]
            if e["type"] == 5:
                raw = C.lz_decompress(payload)
                where = "LZ"
            elif e["type"] == 0:
                raw = payload
                where = "raw"
            else:
                continue
            i = raw.find(pat)
            while i >= 0:
                hdr_ok = None
                if e["p1"] == STAGE_HDR_VA and len(raw) >= 0x2c:
                    hdr_ok = struct.unpack_from("<I", raw, 0x28)[0] == e["p1"] + i
                hits.append({"slot": e["slot"], "type": e["type"], "dest_va": e["p1"],
                             "offset_in_chunk": i, "table_va": e["p1"] + i, "storage": where,
                             "pairs": pairs, "stage_header_ptr_matches": hdr_ok})
                i = raw.find(pat, i + 1)
    return pairs, hits


STAGE_HDR_VA = 0x5E0000   # stage header; dword +0x28 -> camera table (read by 0x481e10 into G+0xa6c)


def _safe_out(path: str):
    """Refuse to write anywhere inside the game installation (read-only by policy)."""
    ap = os.path.normcase(os.path.abspath(path))
    if os.path.normcase(os.path.abspath(r"C:/GOG Games")) in ap:
        raise SystemExit(f"refusing to write inside the game folder: {path}")


def replace_entry(src_path: str, out_path: str, slot: int, group: int = 0,
                  raw: bytes | None = None, png: str | None = None, mode: str = "optimal",
                  allow_layout_change: bool = False, log=print) -> bytes:
    """Replace one entry of a container and write the rebuilt container to out_path.
    raw: new DECODED content for LZ entries / new payload for raw entries.
    png: 320x240 (or VRAM-rect sized) image converted to PS1 BGR555 for 16-bit entries."""
    _safe_out(out_path)
    data = open(src_path, "rb").read()
    kind = kind_of(src_path)
    groups = parse(data, kind)
    g = groups[group]
    e = [x for x in g["entries"] if x["slot"] == slot][0]
    payload = data[e["offset"]: e["offset"] + e["size"]]
    lz = entry_is_lz(kind, slot, e["type"])
    old_dec = C.lz_decompress(payload) if lz else payload
    if png is not None:
        # Only true 16-bit pictures can come from a PNG. VRAM texture pages (type 6,
        # DBS slot 3) hold 4/8-bit indexed texels + CLUTs: edit those as raw data
        # (C.detile32/C.tile32 help with the type-6 tile order) and pass --raw.
        if lz and len(old_dec) == 320 * 240 * 2 and e["type"] == 5:
            raw = C.load_png16(png, (320, 240))
        else:
            raise SystemExit("--png is only supported for 320x240 16-bit type-5 pictures "
                             "(e.g. TITLE.DAT slot 5, TITLE2.DAT slot 7); use --raw otherwise")
    if raw is None:
        raise SystemExit("no replacement data")
    if lz and len(raw) != len(old_dec):
        log(f"  NOTE: decoded size changes {len(old_dec)} -> {len(raw)} (make sure the destination can hold it)")
    new_payload = C.lz_compress(raw, mode) if lz else raw
    if lz:
        assert C.lz_decompress(new_payload, len(raw)) == raw
    out = bytearray()
    layout = []
    for gi, gg in enumerate(groups):
        ents = []
        for x in gg["entries"]:
            p = data[x["offset"]: x["offset"] + x["size"]]
            p1 = x["p1"]
            if gi == group and x["slot"] == slot:
                p = new_payload
                if lz:
                    p1 = _check_decoded_size(kind, slot, x["type"], x["p1"], x["p2"], len(raw), log)
            ents.append({"type": x["type"], "p1": p1, "p2": x["p2"],
                         "reserved": x["reserved"], "payload": p})
        if kind == "DBS":
            if len(ents[0]["payload"]) > SECTOR or (len(ents) >= 3 and len(ents[2]["payload"]) > SECTOR):
                raise ContainerError("DBS slot0/slot2 must stay <= 0x800 bytes")
        gb = build_group(ents)
        layout.append(((len(out) // SECTOR, len(gb)), (gg["offset"] // SECTOR, gg["length"])))
        out += gb
    if kind == "DBS" and any(a != b for a, b in layout):
        msg = "DBS group layout changed; run `camsync` to patch the stage ST???.DAT camera table"
        if not allow_layout_change:
            raise SystemExit("ERROR: " + msg + " (or pass --allow-layout-change)")
        log("  WARNING: " + msg)
    log(f"  slot {slot}: payload {len(payload)} -> {len(new_payload)} bytes"
        f"{' (LZSS, decoded ' + str(len(raw)) + ')' if lz else ''}; container {len(data)} -> {len(out)}")
    with open(out_path, "wb") as fh:
        fh.write(out)
    return bytes(out)


def camsync(dat_path: str, old_dbs: str, new_dbs: str, out_dat: str, mode: str = "optimal", log=print):
    """Write a copy of the stage DAT whose camera table matches the group layout of new_dbs."""
    _safe_out(out_dat)
    pairs_old, hits = find_camtable(dat_path, old_dbs)
    if len(hits) != 1:
        raise SystemExit(f"expected exactly one camera table, found {len(hits)}")
    h = hits[0]
    new = parse(open(new_dbs, "rb").read(), "DBS")
    pairs_new = [(g["offset"] // SECTOR, g["length"]) for g in new]
    if len(pairs_new) != len(pairs_old):
        raise SystemExit("camera/group count differs; only in-place table updates are supported")
    data = open(dat_path, "rb").read()
    kind = kind_of(dat_path)
    groups = parse(data, kind)
    out = bytearray()
    for gg in groups:
        ents = []
        for x in gg["entries"]:
            p = data[x["offset"]: x["offset"] + x["size"]]
            if x["slot"] == h["slot"]:
                raw = bytearray(C.lz_decompress(p) if x["type"] == 5 else p)
                o = h["offset_in_chunk"]
                raw[o:o + 8 * len(pairs_new)] = b"".join(struct.pack("<II", a, b) for a, b in pairs_new)
                p = C.lz_compress(bytes(raw), mode) if x["type"] == 5 else bytes(raw)
            ents.append({"type": x["type"], "p1": x["p1"], "p2": x["p2"],
                         "reserved": x["reserved"], "payload": p})
        out += build_group(ents)
    with open(out_dat, "wb") as fh:
        fh.write(out)
    log(f"  camera table @VA 0x{h['table_va']:x} (slot {h['slot']}): {pairs_old} -> {pairs_new}")
    return pairs_old, pairs_new


def roombg(dbs_path: str, group: int, png_path: str, out_path: str, quality: int = 90,
           log=print) -> bytes:
    """Replace one camera background of a .DBS with a 320x240 picture:
      slot 1 <- baseline 4:2:0 JPEG of the picture (what the game decodes into 0x6c1500)
      slot 3 <- (if type 0) occlusion cells regenerated from the NEW picture: for every
                slot-0 record {u8 tx,u8 ty,u16 z} an 18x18 BGR555 crop (16x16 tile + 1 px
                edge-clamped border) at (tx*16-1, ty*16-1); the STP bit (bit 15, used as the
                alpha mask by 0x4be770) is copied from the original cell.
    Type-8 slot 3 (8bpp overlay texture pages, e.g. ST000) is left untouched (warned).
    The group layout usually changes -> run camsync on the stage DAT afterwards."""
    import io
    import numpy as np
    from PIL import Image
    _safe_out(out_path)
    data = open(dbs_path, "rb").read()
    groups = parse(data, "DBS")
    g = groups[group]
    ents = g["entries"]
    im = Image.open(png_path).convert("RGB")
    if im.size != (320, 240):
        raise SystemExit(f"{png_path}: need 320x240, got {im.size}")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, subsampling=2, progressive=False, optimize=False)
    jpg = buf.getvalue()
    new_payload = {1: jpg}
    if len(ents) >= 4 and ents[3]["type"] == 0:
        s0 = data[ents[0]["offset"]: ents[0]["offset"] + ents[0]["size"]]
        cnt = struct.unpack_from("<H", s0, 0)[0]
        recs = [struct.unpack_from("<BBH", s0, 4 + 4 * i) for i in range(cnt)]
        old = np.frombuffer(C.lz_decompress(data[ents[3]["offset"]: ents[3]["offset"] + ents[3]["size"]]), "<u2")
        if old.size != cnt * 324:
            raise SystemExit(f"unexpected occlusion buffer size {old.size} for {cnt} cells")
        # use the JPEG as the game will see it (decoded) so cells and background match
        dec = np.asarray(Image.open(io.BytesIO(jpg)).convert("RGB")).astype(np.uint16)
        bg = (dec[..., 0] >> 3) | ((dec[..., 1] >> 3) << 5) | ((dec[..., 2] >> 3) << 10)
        pad = np.pad(bg, 1, mode="edge")
        cells = old.copy().reshape(cnt, 18, 18)
        for k, (tx, ty, z) in enumerate(recs):
            crop = pad[ty * 16: ty * 16 + 18, tx * 16: tx * 16 + 18]
            cells[k] = (cells[k] & 0x8000) | (crop & 0x7FFF)
        new_payload[3] = C.lz_compress(cells.astype("<u2").tobytes())
        log(f"  slot 3: regenerated {cnt} occlusion cells (STP masks kept)")
    elif len(ents) >= 4:
        log("  WARNING: slot 3 is a VRAM overlay texture page (type %d) - not regenerated; "
            "edit it separately if the art under it changes" % ents[3]["type"])
    out = bytearray()
    for gi, gg in enumerate(groups):
        es = []
        for x in gg["entries"]:
            p = data[x["offset"]: x["offset"] + x["size"]]
            if gi == group and x["slot"] in new_payload:
                p = new_payload[x["slot"]]
            es.append({"type": x["type"], "p1": x["p1"], "p2": x["p2"], "reserved": x["reserved"], "payload": p})
        out += build_group(es)
    with open(out_path, "wb") as fh:
        fh.write(out)
    log(f"  slot 1: JPEG {ents[1]['size']} -> {len(jpg)} bytes (q={quality}); file {len(data)} -> {len(out)}; "
        f"run camsync if the layout changed")
    return bytes(out)


# ----------------------------------------------------------------------------
def _cmd_list(path):
    data = open(path, "rb").read()
    kind = kind_of(path)
    groups = parse(data, kind)
    print(f"{os.path.basename(path)}: {len(data)} bytes, kind {kind}, {len(groups)} group(s)")
    for gi, g in enumerate(groups):
        print(f" group {gi} @0x{g['offset']:x} (sector {g['offset'] // SECTOR}) length 0x{g['length']:x}, "
              f"{len(g['entries'])} entries")
        for e in g["entries"]:
            print(f"   [{e['slot']:2d}] {describe(kind, e)}")


def _cmd_verify(path):
    data = open(path, "rb").read()
    kind = kind_of(path)
    groups = parse(data, kind)
    ok = True
    for gi, g in enumerate(groups):
        for e in g["entries"]:
            if not entry_is_lz(kind, e["slot"], e["type"]):
                continue
            st = {}
            try:
                raw = C.lz_decompress(data[e["offset"]: e["offset"] + e["size"]], stats=st)
            except C.LZError as ex:
                ok = False
                print(f" g{gi} slot {e['slot']}: FAIL {ex}")
                continue
            extra = ""
            if e["type"] == 6:
                x, y, w, h = C.vram_rect(e["p1"], e["p2"])
                exp = C.type6_size(w, h)
                extra = f" (tile grid needs {exp}: {'OK' if exp == len(raw) else 'MISMATCH'})"
            elif kind == "DBS" and e["type"] != 0:
                x, y, w, h = C.vram_rect(e["p1"], e["p2"])
                extra = f" (rect needs {w * h * 2}: {'OK' if w * h * 2 == len(raw) else 'MISMATCH'})"
            elif kind == "DBS":
                extra = f" (p1 malloc size {e['p1']}: {'OK' if e['p1'] == len(raw) else 'MISMATCH'})"
            print(f" g{gi} slot {e['slot']} type {e['type']}: {e['size']} -> {len(raw)} bytes{extra}")
    print("OK" if ok else "ERRORS")
    return ok


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "list":
        _cmd_list(argv[2])
    elif cmd == "extract":
        man = extract(argv[2], argv[3], decode="--no-decode" not in argv, png="--png" in argv)
        n = sum(len(g["entries"]) for g in man["groups"])
        print(f"extracted {n} entries from {len(man['groups'])} group(s) -> {argv[3]}/manifest.json")
    elif cmd == "rebuild":
        rc = "changed"
        mode = "optimal"
        if "--recompress" in argv:
            rc = argv[argv.index("--recompress") + 1]
        if "--mode" in argv:
            mode = argv[argv.index("--mode") + 1]
        out = rebuild(argv[2], argv[3], rc, mode)
        man = json.load(open(argv[2]))
        same = sha1(out) == man.get("source_sha1")
        print(f"wrote {argv[3]} ({len(out)} bytes); identical to original: {same}")
    elif cmd == "verify":
        return 0 if _cmd_verify(argv[2]) else 1
    elif cmd == "camtable":
        pairs, hits = find_camtable(argv[2], argv[3])
        print("DBS groups (sector, length):", [(a, hex(b)) for a, b in pairs])
        if not hits:
            print("camera table not found")
        for h in hits:
            print(f"found in slot {h['slot']} (type {h['type']}, {h['storage']}, dest VA 0x{h['dest_va']:x}) "
                  f"at chunk offset 0x{h['offset_in_chunk']:x} -> runtime VA 0x{h['table_va']:x}; "
                  f"stage header (+0x28) points here: {h['stage_header_ptr_matches']}")
    elif cmd == "replace":
        # replace <in> <out> --slot N [--group G] (--png f | --raw f) [--mode m] [--allow-layout-change]
        def opt(name, default=None):
            return argv[argv.index(name) + 1] if name in argv else default
        slot = int(opt("--slot"))
        group = int(opt("--group", "0"))
        raw = open(opt("--raw"), "rb").read() if opt("--raw") else None
        replace_entry(argv[2], argv[3], slot, group, raw=raw, png=opt("--png"),
                      mode=opt("--mode", "optimal"),
                      allow_layout_change="--allow-layout-change" in argv)
        print(f"wrote {argv[3]}")
    elif cmd == "camsync":
        # camsync <orig ST.DAT> <orig ST.DBS> <new ST.DBS> <out ST.DAT>
        camsync(argv[2], argv[3], argv[4], argv[5])
        print(f"wrote {argv[5]}")
    elif cmd == "roombg":
        # roombg <ST.DBS> <group> <new320x240.png> <out.DBS> [--quality 90]
        q = int(argv[argv.index("--quality") + 1]) if "--quality" in argv else 90
        roombg(argv[2], int(argv[3]), argv[4], argv[5], q)
        print(f"wrote {argv[5]}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
