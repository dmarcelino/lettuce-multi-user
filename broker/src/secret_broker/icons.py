"""App icons, drawn on first use so the repo carries no binary files: a green
square with a white key-hole shape (full-bleed, so it also works as maskable)."""

from __future__ import annotations

import struct
import zlib
from functools import cache

BG = (46, 125, 50)
FG = (255, 255, 255)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _pixel(x: float, y: float) -> tuple[int, int, int]:
    # Coordinates in 0..1. Key hole: a circle above a tapering stem.
    cx, cy = 0.5, 0.42
    if (x - cx) ** 2 + (y - cy) ** 2 <= 0.12**2:
        return FG
    if 0.42 <= y <= 0.72 and abs(x - cx) <= 0.04 + (y - 0.42) * 0.12:
        return FG
    return BG


@cache
def png(size: int) -> bytes:
    rows = bytearray()
    for j in range(size):
        rows.append(0)
        for i in range(size):
            rows.extend(_pixel((i + 0.5) / size, (j + 0.5) / size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )
