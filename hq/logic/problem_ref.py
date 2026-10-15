"""Normalized identity of a Problems row: its Problem Key and its Link.

Rows created by hand (a form, a coach) and rows imported from a source can name the
same problem in different ways. Two rows of the same member are one solve when their
normalized Problem Key or their normalized Link is equal.
"""

from __future__ import annotations

import re

_CF_PROBLEMSET = re.compile(r"codeforces\.com/problemset/problem/(\d+)/([a-z0-9]+)")
_CF_CONTEST = re.compile(r"codeforces\.com/contest/(\d+)/problem/([a-z0-9]+)")


def norm_key(value: str | None) -> str:
    """Problem Key, trimmed and lower-cased ("" when empty)."""
    return (value or "").strip().lower()


def norm_link(url: str | None) -> str:
    """A problem URL in one form: no scheme, no "www.", no trailing slash, lower case.
    Codeforces "problemset/problem/X/Y" is written as "contest/X/problem/Y"."""
    text = (url or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = _CF_PROBLEMSET.search(text)
    if match:
        return f"codeforces.com/contest/{match.group(1)}/problem/{match.group(2)}"
    return text


def identities(key: str | None, link: str | None) -> set[str]:
    """Every identity a row answers to: its normalized key and its normalized link.
    The two are kept apart by prefix so a key can never equal a link by accident."""
    out: set[str] = set()
    nk = norm_key(key)
    nl = norm_link(link)
    if nk:
        out.add(f"key:{nk}")
    if nl:
        out.add(f"link:{nl}")
    return out
