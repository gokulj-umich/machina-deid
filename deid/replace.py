"""Fast multi-pattern identity replacement on raw file bytes.

Bytes are viewed as latin-1 text (a 1:1 byte<->char mapping), so the same code
works for text and binary files and never changes bytes it does not replace.
Each identity is searched in UTF-8 and UTF-16LE spellings, case-insensitively,
with word-boundary checks so short names never match inside other words.

Two modes:
  free    : replacement may change length (plain-text formats such as STEP,
            DXF, SVG, Markdown)
  fixed   : replacement keeps the exact byte length (binary formats, PDFs,
            IGES/SAT/Parasolid text with length-prefixed strings), so internal
            offsets and record lengths stay valid.
"""
from __future__ import annotations

import re

import ahocorasick

from .identities import fit, pseudo

_ALNUM = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24})(?![A-Za-z0-9\-])")
USERPATH_RE = re.compile(
    r"(?i)((?:[a-z]:(?:\\\\|\\|/)+(?:users|documents and settings)(?:\\\\|\\|/)+)|(?<![A-Za-z0-9])/home/|(?<![A-Za-z0-9])/Users/)"
    r"([A-Za-z0-9][A-Za-z0-9 ._\-]{1,40}?)(?=\\|/)"
)
GENERIC_USERS = {"public", "default", "all users", "administrator", "admin", "user", "users", "shared", "guest", "runner", "root", "ubuntu"}
# e-mail addresses that are format examples, not identities
EMAIL_KEEP_DOMAINS = {"example.com", "example.org", "example.net"}


class Replacer:
    def __init__(self, entries: list[tuple[str, str, str]]):
        """entries: (spelling, kind, canonical identity)."""
        self.A = ahocorasick.Automaton()
        n = 0
        for spelling, kind, ident in entries:
            rep = pseudo(kind, ident)
            for enc in ("utf-8", "utf-16-le"):
                try:
                    b = spelling.encode(enc)
                except UnicodeEncodeError:
                    continue
                key = b.decode("latin-1").lower()
                if len(key) < 3:
                    continue
                wide = enc == "utf-16-le"
                prev = self.A.get(key, None)
                # first (seed/most specific) definition wins
                if prev is None:
                    self.A.add_word(key, (len(key), rep, wide))
                    n += 1
        self.n = n
        if n:
            self.A.make_automaton()

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _bounded(s: str, start: int, end: int, wide: bool) -> bool:
        step = 2 if wide else 1
        b = start - step
        if b >= 0 and s[b] in _ALNUM and (not wide or s[b + 1] == "\x00"):
            return False
        if end < len(s) and s[end] in _ALNUM and (not wide or (end + 1 < len(s) and s[end + 1] == "\x00")):
            return False
        return True

    def spans(self, s: str, low: str, min_len: int = 0):
        out = []
        if not self.n:
            return out
        for end_i, (L, rep, wide) in self.A.iter(low):
            if L < min_len:
                continue
            start = end_i - L + 1
            end = end_i + 1
            if wide and start % 1:
                continue
            if self._bounded(s, start, end, wide):
                out.append((start, end, rep, wide))
        return out


def _encode_rep(rep: str, orig: str, wide: bool, fixed: bool) -> str:
    """Return replacement as a latin-1 'byte string'."""
    if wide:
        if fixed:
            rep = fit(rep, len(orig) // 2)
        return rep.encode("utf-16-le").decode("latin-1")
    if fixed:
        rep = fit(rep, len(orig))
    return rep.encode("utf-8").decode("latin-1")


BINARY_MIN = 6  # shorter names could match by chance inside compressed bytes


def apply(data: bytes, rep: Replacer, fixed: bool, extra: list | None = None, text_rules: bool = True,
          binary: bool = False) -> tuple[bytes, int]:
    """Replace identities in data. Returns (new_bytes, number_of_replacements).

    binary=True: data may contain compressed/random bytes, so only long names
    are matched and pattern rules (e-mails, user paths) are skipped.
    """
    if binary:
        fixed, text_rules = True, False
    s = data.decode("latin-1")
    low = s.lower()
    cands = rep.spans(s, low, BINARY_MIN if binary else 0)
    if extra:
        cands.extend(extra)
    if text_rules:
        for m in EMAIL_RE.finditer(s):
            dom = m.group(2).lower()
            if dom in EMAIL_KEEP_DOMAINS or dom.endswith(".example"):
                continue
            cands.append((m.start(), m.end(), pseudo("email", m.group(0)), False))
        for m in USERPATH_RE.finditer(s):
            u = m.group(2).strip()
            if u.lower() in GENERIC_USERS:
                continue
            cands.append((m.start(2), m.end(2), pseudo("person", u), False))
    if not cands:
        return data, 0
    # leftmost-longest, non-overlapping
    cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    out = []
    pos = 0
    count = 0
    for start, end, r, wide in cands:
        if start < pos:
            continue
        out.append(s[pos:start])
        out.append(_encode_rep(r, s[start:end], wide, fixed))
        pos = end
        count += 1
    out.append(s[pos:])
    return "".join(out).encode("latin-1"), count


def apply_text(text: str, rep: Replacer) -> str:
    """Replace identities in a short unicode string (file/folder names, metadata)."""
    b, n = apply(text.encode("utf-8"), rep, fixed=False)
    if not n:
        return text
    return b.decode("utf-8", errors="surrogateescape")
