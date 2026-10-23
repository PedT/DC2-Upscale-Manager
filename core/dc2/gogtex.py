"""GOG ddraw.dll texture-replacement helpers (reverse-engineered, read-only on game files).
 - name  = "%08X.dat" % zlib.crc32(<DDraw-side texture buffer as uploaded by the game>)
 - file  = PNG/JPG/DDS/BMP bytes, obfuscated with an additive cipher:
           enc[i] = plain[i] + key[i & 15] + (i & 0xFF) + (len & 0xFF)   (mod 256)
 - key   = ddraw.dll .data @ 0x1816e4f8 (16 bytes)
"""
import sys, os, zlib
import numpy as np
KEY = bytes.fromhex("fe3dab09745ce0877c649b2916c20645")

def decrypt(b):
    n = len(b); a = np.frombuffer(b, dtype=np.uint8).astype(np.int32)
    i = np.arange(n)
    k = np.frombuffer(KEY, dtype=np.uint8).astype(np.int32)[i & 15]
    return ((a - (k + (i & 0xFF) + (n & 0xFF))) & 0xFF).astype(np.uint8).tobytes()

def encrypt(b):
    n = len(b); a = np.frombuffer(b, dtype=np.uint8).astype(np.int32)
    i = np.arange(n)
    k = np.frombuffer(KEY, dtype=np.uint8).astype(np.int32)[i & 15]
    return ((a + (k + (i & 0xFF) + (n & 0xFF))) & 0xFF).astype(np.uint8).tobytes()

def bg_texture_bytes(bgr555, depth):
    """Dino2.exe 0x4be120 conversion of the 320x240 PS1 BGR555 background into the
    primary-surface format reported by the GOG wrapper (X8R8G8B8 @32bpp, R5G6B5 @16bpp)."""
    p = np.frombuffer(bgr555[:153600], dtype="<u2").astype(np.uint32)
    r5, g5, b5 = p & 31, (p >> 5) & 31, (p >> 10) & 31
    if depth == 32:
        return ((r5 << 19) | (g5 << 11) | (b5 << 3)).astype("<u4").tobytes()
    if depth == 16:
        return ((r5 << 11) | (g5 << 6) | b5).astype("<u2").tobytes()
    raise ValueError(depth)

def bg_crc(bgr555, depth):
    return zlib.crc32(bg_texture_bytes(bgr555, depth))

if __name__ == "__main__":
    # self-test: roundtrip GOG's own replacement files
    for nm in ("9A8B79FC", "C49FC969"):
        enc = open(f"C:/GOG Games/Dino Crisis 2/Data/{nm}.DAT", "rb").read()
        dec = decrypt(enc)
        assert dec[:8] == b"\x89PNG\r\n\x1a\n"
        assert encrypt(dec) == enc
        print(nm, "decrypt->PNG ok, re-encrypt identical:", encrypt(dec) == enc)
