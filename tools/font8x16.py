#!/usr/bin/env python3
"""Merge Terminus glyphs into the existing 8x16 console font table."""

import argparse
from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "kernel/src/dev/font8x16.bin"
FONT_MAP = ROOT / "kernel/src/dev/font8x16.map"
TERMINUS = ROOT / "kernel/src/dev/font8x16.terminus.bdf"
GLYPH_HEIGHT = 16
ASCII_SLOTS = 128


def read_bdf(path):
    """Return Unicode codepoint -> 16-byte bitmap from an 8x16 BDF."""
    lines = path.read_text(encoding="ascii").splitlines()
    glyphs = {}
    encoding = None
    bitmap = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("STARTCHAR "):
            encoding = None
            bitmap = None
        elif line.startswith("ENCODING "):
            encoding = int(line.split()[1])
        elif line == "BITMAP":
            rows = lines[index + 1:index + 1 + GLYPH_HEIGHT]
            if len(rows) != GLYPH_HEIGHT or any(len(row) != 2 for row in rows):
                raise ValueError("Terminus source contains a non-8x16 glyph")
            bitmap = bytes(int(row, 16) for row in rows)
            index += GLYPH_HEIGHT
        elif line == "ENDCHAR" and encoding is not None and encoding >= 0:
            if bitmap is None:
                raise ValueError(f"U+{encoding:04X} has no bitmap")
            glyphs[encoding] = bitmap
        index += 1
    return glyphs


def merge(font, codepoints, terminus):
    merged = bytearray(font)
    replaced = 0
    for slot, codepoint in enumerate(codepoints):
        glyph = terminus.get(codepoint)
        if glyph is None:
            continue
        start = slot * GLYPH_HEIGHT
        merged[start:start + GLYPH_HEIGHT] = glyph
        replaced += 1
    return bytes(merged), replaced


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify that every available Terminus glyph is installed")
    args = parser.parse_args()

    mapping = FONT_MAP.read_bytes()
    if len(mapping) % 4 != 0:
        raise ValueError("font map size is not a multiple of four")
    codepoints = list(range(ASCII_SLOTS))
    codepoints.extend(value[0] for value in struct.iter_unpack("<I", mapping))

    font = FONT.read_bytes()
    expected_size = len(codepoints) * GLYPH_HEIGHT
    if len(font) != expected_size:
        raise ValueError(f"font is {len(font)} bytes; expected {expected_size}")

    terminus = read_bdf(TERMINUS)
    merged, replaced = merge(font, codepoints, terminus)
    fallback = len(codepoints) - replaced

    if args.check:
        if merged != font:
            raise SystemExit("font8x16.bin does not contain every available Terminus glyph")
        print(f"ok: {replaced} Terminus slots, {fallback} fallback slots")
        return

    FONT.write_bytes(merged)
    print(f"wrote {FONT}: {replaced} Terminus slots, {fallback} fallback slots")


if __name__ == "__main__":
    main()
