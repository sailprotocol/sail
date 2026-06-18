"""Generate a simple 1024x1024 RGB PNG icon source (no external deps).
Usage: python desktop/make_icon.py <out.png>   then: cargo tauri icon <out.png>"""
import struct
import sys
import zlib

W = H = 1024
BG = (14, 17, 22)      # app background
FG = (247, 147, 26)    # bitcoin orange


def color(x: int, y: int):
    cx, cy = W // 2, H // 2
    dx, dy = x - cx, y - cy
    # a rough lightning bolt: two offset diagonal bands
    if abs(dx + dy) < 110 and -300 < dy < 300:
        return FG
    if abs(dx + dy - 220) < 90 and -260 < dx < 60:
        return FG
    return BG


raw = bytearray()
for y in range(H):
    raw.append(0)  # filter type 0 for the scanline
    for x in range(W):
        raw += bytes(color(x, y))


def chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
png += chunk(b"IEND", b"")
pathlib_out = sys.argv[1] if len(sys.argv) > 1 else "icon-src.png"
with open(pathlib_out, "wb") as f:
    f.write(png)
print("wrote", pathlib_out)
