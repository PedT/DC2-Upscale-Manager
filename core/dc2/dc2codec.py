"""dc2codec - Dino Crisis 2 (PC, Capcom 2002 / GOG) chunk codec.

All compressed chunk kinds of the DAT/DBS/TEX container use ONE algorithm: a
byte-oriented LZSS variant decoded by Dino2.exe function 0x4041f0
(signature: void lz_decode(u8 *dst, const u8 *src, int src_size)).

Format (reverse engineered from 0x4041f0, verified on every LZ chunk of the game):
  * The stream is a sequence of groups: one FLAG byte followed by up to 8 items.
  * Flag bits are consumed LSB first. bit=1 -> LITERAL: copy 1 byte.
                                       bit=0 -> MATCH: 2 bytes lo, hi:
        distance = lo | ((hi & 0x0F) << 8)          (1..4095, relative to the
                                                      current output position)
        length   = (hi >> 4) + 2                     (2..17)
        copy `length` bytes one at a time from out[pos - distance] (overlap OK).
  * There is NO end marker and NO stored output size. The decoder keeps a
    counter initialised with the COMPRESSED size (the table entry's size field)
    and subtracts 1 per flag byte, 1 per literal and 2 per match; it stops when
    the counter is <= 0 (checked after each item). Output size is implicit.
  * Consequently an encoder must never end the stream with a flag byte that is
    not followed by at least one item, and the container size field must equal
    the exact compressed length.

Which container entries are LZSS-compressed (see dc2pack.py):
  * generic loader (all .DAT / .TEX files): types 5 and 6
        type 5 -> lz_decode(dst = (u8*)p1, ...)            (memory)
        type 6 -> lz_decode(0x5a0000, ...) then 32x32-halfword tiles -> VRAM rect
  * DBS camera loader (0x403ac0): entry #3 of every group, whatever its type
        type 8 (or any non-zero) -> lz_decode(0x5a0000) then ONE linear VRAM upload
        type 0                    -> lz_decode(malloc(p1)) (RAM buffer, p1 = size)

API
  lz_decompress(src, expected_size=None, strict=True) -> bytes
  lz_compress(raw, mode="optimal"|"lazy"|"greedy"|"literal") -> bytes
  decompress(data, ctype, expected_size=None) -> bytes   (raw types returned unchanged)
  compress(raw, ctype, mode="optimal", chain=256) -> bytes (raw types returned unchanged)
  is_lz_type(ctype) ; detile32(raw, w, h) ; tile32(lin, w, h)
  bgr555_to_rgb(raw, w, h) ; rgb_to_bgr555(rgb, ...) ; save_png16 / load_png16

CLI
  python -I dc2codec.py d <type> <in> <out> [expected_size]
  python -I dc2codec.py c <type> <in> <out> [mode] [--chain N]
  python -I dc2codec.py png16 <in.raw> <out.png> <w> <h>        (PS1 BGR555 -> PNG)
  python -I dc2codec.py raw16 <in.png> <out.raw> [--stp-black]   (PNG -> PS1 BGR555)
"""
from __future__ import annotations

import struct
import sys

LZ_TYPES = (5, 6, 8)
MAX_DIST = 0xFFF          # 12-bit distance field; 0 is never valid
MIN_LEN = 2
MAX_LEN = 17              # 4-bit length field + 2
DEFAULT_CHAIN = 256       # hash-chain depth (64: fast; 100000: exhaustive, slow)


class LZError(ValueError):
    pass


def is_lz_type(ctype: int) -> bool:
    return ctype in LZ_TYPES


# ----------------------------------------------------------------------------
# Decoder - exact emulation of Dino2.exe 0x4041f0
# ----------------------------------------------------------------------------
def lz_decompress(src: bytes, expected_size: int | None = None, strict: bool = True,
                  declared_size: int | None = None, stats: dict | None = None) -> bytes:
    """Decode an LZSS stream exactly like the game.

    declared_size: the counter the game starts with (= table entry size). Defaults
    to len(src). Bytes read beyond len(src) are taken as 0 (container padding).
    strict: raise LZError on anything the game would handle by reading memory
    outside the produced output (distance 0 or distance > bytes produced so far)
    or on reading past the end of src.
    """
    n = len(src)
    cnt = n if declared_size is None else declared_size
    out = bytearray()
    i = 0
    bits = 0
    flags = 0
    overrun = False
    bad_refs = 0
    nlit = nmatch = 0
    if cnt <= 0:
        return b""
    while True:
        if bits == 0:
            flags = src[i] if i < n else 0
            i += 1
            cnt -= 1
            bits = 8
        bits -= 1
        cnt -= 1
        if flags & 1:
            if i >= n:
                overrun = True
            out.append(src[i] if i < n else 0)
            i += 1
            nlit += 1
        else:
            cnt -= 1
            lo = src[i] if i < n else 0
            hi = src[i + 1] if i + 1 < n else 0
            if i + 1 >= n:
                overrun = True
            i += 2
            dist = lo | ((hi & 0x0F) << 8)
            length = (hi >> 4) + 2
            pos = len(out)
            start = pos - dist
            nmatch += 1
            if dist == 0 or start < 0:
                bad_refs += 1
                if strict:
                    raise LZError(f"match at out=0x{pos:x} in=0x{i - 2:x} refers to "
                                  f"distance {dist} (only {pos} bytes produced)")
                for _ in range(length):  # game reads foreign memory; we use 0
                    p = len(out) - dist
                    out.append(out[p] if 0 <= p < len(out) else 0)
            elif dist >= length:
                out += out[start:start + length]
            else:
                for k in range(length):
                    out.append(out[start + k])
        flags >>= 1
        if cnt <= 0:
            break
    if overrun and strict:
        raise LZError("stream reads past the end of the compressed data "
                      "(dangling flag byte or truncated item)")
    if stats is not None:
        stats.update(consumed=i, literals=nlit, matches=nmatch, bad_refs=bad_refs,
                     overrun=overrun, out_size=len(out))
    if expected_size is not None and len(out) != expected_size:
        if strict:
            raise LZError(f"decoded {len(out)} bytes, expected {expected_size}")
    return bytes(out)


# ----------------------------------------------------------------------------
# Encoder
# ----------------------------------------------------------------------------
def _emit(items, raw: bytes) -> bytes:
    """items: list of (pos, length, distance) ; length==1 & dist==0 => literal."""
    out = bytearray()
    k = 0
    nitems = len(items)
    while k < nitems:
        flag_idx = len(out)
        out.append(0)
        flag = 0
        for b in range(8):
            if k >= nitems:
                break
            pos, length, dist = items[k]
            if dist == 0:
                flag |= 1 << b
                out.append(raw[pos])
            else:
                assert MIN_LEN <= length <= MAX_LEN and 1 <= dist <= MAX_DIST
                out.append(dist & 0xFF)
                out.append(((length - 2) << 4) | (dist >> 8))
            k += 1
        out[flag_idx] = flag
    return bytes(out)


def _longest_matches(raw: bytes, chain: int = DEFAULT_CHAIN):
    """For every position return (best_len, best_dist) of the longest match
    (2..17 bytes, distance 1..4095) found with hash chains (exact search for
    length-2 matches via the most recent occurrence)."""
    n = len(raw)
    blen = [0] * n
    bdist = [0] * n
    head3: dict[bytes, list[int]] = {}
    last2: dict[bytes, int] = {}
    mv = raw  # bytes supports fast slicing
    for i in range(n):
        rem = n - i
        best_l = 0
        best_d = 0
        if rem >= 3:
            key = mv[i:i + 3]
            lst = head3.get(key)
            if lst is not None:
                maxl = MAX_LEN if rem >= MAX_LEN else rem
                tgt = mv[i:i + maxl]
                checked = 0
                for idx in range(len(lst) - 1, -1, -1):
                    p = lst[idx]
                    d = i - p
                    if d > MAX_DIST:
                        # older entries are even farther: prune list
                        del lst[:idx + 1]
                        break
                    # length of common prefix (3 already equal)
                    if mv[p:p + maxl] == tgt:
                        l = maxl
                    else:
                        l = 3
                        while l < maxl and mv[p + l] == tgt[l]:
                            l += 1
                    if l > best_l:
                        best_l = l
                        best_d = d
                        if l == maxl:
                            break
                    checked += 1
                    if checked >= chain:
                        break
                lst.append(i)
            else:
                head3[key] = [i]
        if best_l < 3 and rem >= 2:
            k2 = mv[i:i + 2]
            p = last2.get(k2)
            if p is not None and i - p <= MAX_DIST:
                best_l = 2
                best_d = i - p
        if rem >= 2:
            last2[mv[i:i + 2]] = i
        blen[i] = best_l
        bdist[i] = best_d
    return blen, bdist


def lz_compress(raw: bytes, mode: str = "optimal", chain: int = DEFAULT_CHAIN) -> bytes:
    """Encode raw bytes into the game's LZSS format.

    modes: "literal" (no matches, size n + ceil(n/8)), "greedy", "lazy",
           "optimal" (bit-cost dynamic programming over the longest-match table;
           smallest output, default).
    """
    raw = bytes(raw)
    n = len(raw)
    if n == 0:
        return b""
    if mode == "literal":
        return _emit([(i, 1, 0) for i in range(n)], raw)
    blen, bdist = _longest_matches(raw, chain)
    items = []
    if mode in ("greedy", "lazy"):
        i = 0
        while i < n:
            l = blen[i]
            if l >= MIN_LEN:
                if mode == "lazy" and i + 1 < n and blen[i + 1] > l + 1:
                    items.append((i, 1, 0))
                    i += 1
                    continue
                items.append((i, l, bdist[i]))
                i += l
            else:
                items.append((i, 1, 0))
                i += 1
    elif mode == "optimal":
        INF = 1 << 60
        cost = [0] * (n + 1)
        choice = [1] * n
        for i in range(n - 1, -1, -1):
            best = cost[i + 1] + 9
            bc = 1
            l = blen[i]
            if l >= MIN_LEN:
                for L in range(l, MIN_LEN - 1, -1):
                    c = cost[i + L] + 17
                    if c < best:
                        best = c
                        bc = L
            cost[i] = best
            choice[i] = bc
        i = 0
        while i < n:
            L = choice[i]
            if L == 1:
                items.append((i, 1, 0))
                i += 1
            else:
                items.append((i, L, bdist[i]))
                i += L
    else:
        raise ValueError("unknown mode " + mode)
    return _emit(items, raw)


# ----------------------------------------------------------------------------
# Type-level API
# ----------------------------------------------------------------------------
def decompress(data: bytes, ctype: int, expected_size: int | None = None) -> bytes:
    """Decompress a chunk payload of container type `ctype`.
    Types 5/6/8 are LZSS; every other type is stored raw and returned unchanged.
    (NB: in .DBS files entry #3 of a group is LZSS even when its type is 0 - use
    lz_decompress() directly for that case, dc2pack handles it.)"""
    if is_lz_type(ctype):
        return lz_decompress(data, expected_size)
    return bytes(data)


def compress(raw: bytes, ctype: int, mode: str = "optimal", chain: int = DEFAULT_CHAIN) -> bytes:
    if is_lz_type(ctype):
        return lz_compress(raw, mode, chain)
    return bytes(raw)


# ----------------------------------------------------------------------------
# VRAM helpers
# ----------------------------------------------------------------------------
def vram_rect(p1: int, p2: int):
    """(x, y, w, h) in 16-bit VRAM units from table params p1/p2."""
    return p1 & 0xFFFF, (p1 >> 16) & 0xFFFF, p2 & 0xFFFF, (p2 >> 16) & 0xFFFF


def type6_size(w: int, h: int) -> int:
    """Bytes consumed by the type-6 (and type-1) tile uploader for a w x h rect."""
    return ((w + 31) & ~31) * ((h + 31) & ~31) * 2


def detile32(raw: bytes, w: int, h: int) -> bytes:
    """Type 6/1 layout (32x32-halfword tiles, row-major tile order) -> linear
    w x h halfword image (rows of w*2 bytes) exactly as the game places it."""
    W = (w + 31) & ~31
    H = (h + 31) & ~31
    lin = bytearray(W * H * 2)
    t = 0
    for ty in range(0, H, 32):
        for tx in range(0, W, 32):
            base = t * 0x800
            for r in range(32):
                src = raw[base + r * 64: base + r * 64 + 64]
                if len(src) < 64:
                    src = src + bytes(64 - len(src))
                o = ((ty + r) * W + tx) * 2
                lin[o:o + 64] = src
            t += 1
    if W == w and H == h:
        return bytes(lin)
    out = bytearray()
    for y in range(h):
        out += lin[y * W * 2: y * W * 2 + w * 2]
    return bytes(out)


def tile32(lin: bytes, w: int, h: int) -> bytes:
    """Inverse of detile32: linear w x h halfwords -> 32x32 tile stream (pads with 0)."""
    W = (w + 31) & ~31
    H = (h + 31) & ~31
    full = bytearray(W * H * 2)
    for y in range(h):
        full[y * W * 2: y * W * 2 + w * 2] = lin[y * w * 2: (y + 1) * w * 2]
    out = bytearray()
    for ty in range(0, H, 32):
        for tx in range(0, W, 32):
            for r in range(32):
                o = ((ty + r) * W + tx) * 2
                out += full[o:o + 64]
    return bytes(out)


# ----------------------------------------------------------------------------
# PS1 BGR555 <-> PNG helpers (bit 0-4 R, 5-9 G, 10-14 B, bit 15 STP)
# ----------------------------------------------------------------------------
def bgr555_to_rgb(raw: bytes, w: int, h: int):
    import numpy as np
    px = np.frombuffer(raw[: w * h * 2], dtype="<u2").reshape(h, w)

    def ex(v):
        return ((v << 3) | (v >> 2)).astype(np.uint8)
    r = ex(px & 0x1F)
    g = ex((px >> 5) & 0x1F)
    b = ex((px >> 10) & 0x1F)
    return np.dstack([r, g, b]), px


def save_png16(raw: bytes, w: int, h: int, path: str, alpha_for_zero: bool = False):
    from PIL import Image
    import numpy as np
    rgb, px = bgr555_to_rgb(raw, w, h)
    if alpha_for_zero:
        a = np.where(px == 0, 0, 255).astype(np.uint8)
        Image.fromarray(np.dstack([rgb, a]), "RGBA").save(path)
    else:
        Image.fromarray(rgb, "RGB").save(path)


def rgb_to_bgr555(rgb, stp_black: bool = False) -> bytes:
    """rgb: HxWx3 uint8 array. Rounds to 5 bits; STP (bit 15) left 0 like the
    original title/room pictures (the background renderer 0x4be120 ignores STP).
    stp_black=True writes would-be 0x0000 pixels as 0x8000 (opaque black) - only
    useful for data used as PS1 textures, where 0x0000 means transparent."""
    import numpy as np
    a = np.asarray(rgb).astype(np.uint16)
    r = (a[..., 0] * 31 + 127) // 255
    g = (a[..., 1] * 31 + 127) // 255
    b = (a[..., 2] * 31 + 127) // 255
    px = (r | (g << 5) | (b << 10)).astype(np.uint16)
    if stp_black:
        px[px == 0] = 0x8000
    return px.astype("<u2").tobytes()


def load_png16(path: str, size=None, stp_black: bool = False) -> bytes:
    from PIL import Image
    import numpy as np
    im = Image.open(path).convert("RGB")
    if size is not None and im.size != tuple(size):
        raise ValueError(f"{path}: image is {im.size}, expected {tuple(size)}")
    return rgb_to_bgr555(np.asarray(im), stp_black)


# ----------------------------------------------------------------------------
def _safe_out(path: str):
    """Refuse to write anywhere inside the game installation (read-only by policy)."""
    import os
    ap = os.path.normcase(os.path.abspath(path))
    if os.path.normcase(os.path.abspath(r"C:/GOG Games")) in ap:
        raise SystemExit(f"refusing to write inside the game folder: {path}")


def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    outp = {"d": 4, "c": 4, "png16": 3, "raw16": 3}.get(cmd)
    if outp is not None and len(argv) > outp:
        _safe_out(argv[outp])
    if cmd == "d":
        t = int(argv[2], 0)
        data = open(argv[3], "rb").read()
        exp = int(argv[5], 0) if len(argv) > 5 else None
        st = {}
        if is_lz_type(t):
            out = lz_decompress(data, exp, stats=st)
        else:
            out = data
        open(argv[4], "wb").write(out)
        print(f"type {t}: {len(data)} -> {len(out)} bytes {st}")
    elif cmd == "c":
        t = int(argv[2], 0)
        data = open(argv[3], "rb").read()
        mode = argv[5] if len(argv) > 5 and not argv[5].startswith("--") else "optimal"
        chain = int(argv[argv.index("--chain") + 1]) if "--chain" in argv else DEFAULT_CHAIN
        out = compress(data, t, mode, chain) if is_lz_type(t) else data
        if is_lz_type(t):
            chk = lz_decompress(out, len(data))  # self-check
            assert chk == data
        open(argv[4], "wb").write(out)
        print(f"type {t}: {len(data)} -> {len(out)} bytes (mode {mode}, verified)")
    elif cmd == "png16":
        data = open(argv[2], "rb").read()
        w, h = int(argv[4]), int(argv[5])
        save_png16(data, w, h, argv[3])
        print("wrote", argv[3])
    elif cmd == "raw16":
        raw = load_png16(argv[2], stp_black="--stp-black" in argv)
        open(argv[3], "wb").write(raw)
        print("wrote", argv[3], len(raw), "bytes")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
