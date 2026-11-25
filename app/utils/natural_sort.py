"""Natural order for titles: numbers compare by value ("Part 2" before "Part 10"), letters without regard to case.
Registered with SQLite as the NATURAL collation, which the library's Title sort uses."""
from __future__ import annotations
import re

_PIECES = re.compile(r'(\d+)')


def natural_key(text) -> tuple:
    pieces = _PIECES.split((text or '').casefold())
    return tuple((0, int(piece), piece) if index % 2 else (1, 0, piece) for index, piece in enumerate(pieces) if piece)


def natural_compare(a, b) -> int:
    ka, kb = natural_key(a), natural_key(b)
    return (ka > kb) - (ka < kb)
