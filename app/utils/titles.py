"""Title casing for titles LitKeeper makes up itself (from a web address, when the page gave no title)."""
from __future__ import annotations
import re

# Always in capitals, also as a plural or possessive: GF, GFs, GF's, BF, BFs, BF's, MILF, MILFs.
ACRONYMS = re.compile(r"\b(gf|bf|milf)(s|['’]s)?\b", re.I)


def fix_acronyms(title: str) -> str:
    return ACRONYMS.sub(lambda m: m.group(1).upper() + (m.group(2) or '').lower(), title or '')


def title_from_slug(slug: str) -> str:
    """'my-gfs-sister' → 'My GFs Sister'."""
    return fix_acronyms((slug or '').replace('-', ' ').title())
