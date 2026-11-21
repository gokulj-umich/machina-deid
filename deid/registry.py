"""Global identity registry built from the corpus scan (tools/discover.py).

Every identity found anywhere (PDF metadata and title blocks, XMP, PDF
annotations, OLE author / last-saved-by / company, STEP FILE_NAME author and
organization, e-mail addresses, repository URLs) is registered once, before any
file is rewritten, so the same identity gets the same pseudonym in every file
and format, and is also replaced where it appears as plain text elsewhere.
"""
from __future__ import annotations

import collections
import gzip
import json
import re

from .identities import STOP, norm

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
GH_URL = re.compile(r"(?i)\b(?:github|gitlab)\.com/([A-Za-z0-9][A-Za-z0-9\-_.]{1,38})(?:/([A-Za-z0-9\-_.]{2,100}))?")
ACCOUNT = re.compile(r"^\s*([A-Za-z0-9][\w.\-]{1,40})\\([\w.\- ]{1,40}?)(?:\s*\(([^)]*)\))?\s*$")

PERSON_LABELS = r"drawn|drawn by|drn|dr|designed|designed by|design|designer|des|checked|checked by|chk|chkd|chk'd|ck|approved|approved by|appr|appd|app'd|appv'd|engineer|eng|engr|author|prepared by|created by|modified by|revised by|made by|by|drafter|draftsman|mfg|q\.a|qa|reviewed by|responsible"
ORG_LABELS = r"company|customer|client|supplier|vendor|manufacturer|organization|organisation|firm|contractor|owner|institution|university|lab|laboratory"
LABEL_RX = re.compile(
    rf"(?im)(?:^|\s)(?:(?P<p>{PERSON_LABELS})|(?P<o>{ORG_LABELS}))\b[ \t]*(?:name)?[ \t]*:[ \t]*(?P<v>[^\n:]{{2,60}})$"
)
INLINE_BY = re.compile(r"\b(?:[Dd]esigned|[Dd]rawn|[Cc]reated|[Mm]ade|[Dd]eveloped|[Ww]ritten|[Bb]uilt)[ \t\n]+by[ \t\n]+([A-Z][a-zA-Z'\-]+(?:[ \t]+[A-Z][a-zA-Z'\-]+){1,2})")

GENERIC = {
    "unknown", "none", "n/a", "na", "anonymous", "user", "admin", "administrator", "author", "owner", "default", "test",
    "windows user", "valued customer", "registered user", "name", "date", "signature", "title", "scale", "sheet",
    "rev", "revision", "material", "finish", "weight", "size", "dwg", "drawing", "part", "qty", "description",
    "solidworks", "autodesk", "microsoft", "adobe", "creo", "ptc", "catia", "dassault", "freecad", "inventor",
    "student", "student version", "educational", "company", "customer", "draft", "draftsman",
}
WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")
NAME_LIKE = re.compile(r"^(?:[A-Z][a-zA-Z'\-]{1,20}\.?|[A-Z]\.)(?:\s+(?:[A-Z][a-zA-Z'\-]{1,20}\.?|[A-Z]\.|van|von|de|der|da|di|le|la)){0,3}$")


def _clean(v: str) -> str:
    v = re.sub(r"\s+", " ", v or "").strip(" .,:;-_|/\\\t")
    return v


def _ok(v: str, min_len: int = 3) -> bool:
    if len(v) < min_len or len(v) > 60:
        return False
    low = v.lower()
    if low in GENERIC or norm(v) in STOP:
        return False
    if not re.search(r"[A-Za-z]", v):
        return False
    if re.fullmatch(r"[\d\W_]+", v):
        return False
    return True


class Registry:
    def __init__(self):
        self.ids = collections.OrderedDict()  # (kind, norm) -> canonical
        self.spell = {}  # spelling(lower) -> (spelling, kind, canonical)
        self.evidence = collections.Counter()

    def add(self, kind: str, value: str, spellings=(), min_len=3):
        value = _clean(value)
        if not _ok(value, min_len):
            return
        key = (kind, norm(value))
        canon = self.ids.setdefault(key, value)
        self.evidence[key] += 1
        for s in (value, *spellings):
            s = _clean(s)
            if _ok(s, min_len) and s.lower() not in self.spell:
                self.spell[s.lower()] = (s, kind, canon)

    def add_account(self, v: str):
        v = _clean(v)
        if not v or v.lower() in GENERIC:
            return
        m = ACCOUNT.match(v)
        if m:
            host, user = m.group(1), m.group(2)
            self.add("org", "host:" + host.lower(), ())  # canonical host identity
            self.spell.setdefault(host.lower(), (host, "org", "host:" + host.lower()))
            self.add("person", user, (), min_len=2)
            return
        self.add("person", v, (), min_len=2)

    def entries(self) -> list[tuple[str, str, str]]:
        return list(self.spell.values())

    def dump(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            for (kind, _), canon in self.ids.items():
                f.write(f"{kind}\t{self.evidence[(kind, norm(canon))]}\t{canon}\n")


def _from_text(reg: Registry, text: str):
    for m in EMAIL.finditer(text):
        e = m.group(0)
        dom = e.split("@", 1)[1].lower()
        if dom.endswith(("example.com", "example.org", ".example")):
            continue
        reg.add("email", e, (), min_len=5)
        local = e.split("@", 1)[0]
        if "." in local and all(len(p) >= 2 for p in local.split(".")):
            parts = [p.capitalize() for p in local.split(".")]
            reg.add("person", " ".join(parts), ())
    for m in GH_URL.finditer(text):
        owner = m.group(1)
        if owner.lower() not in ("orgs", "users", "about", "features", "topics", "explore", "settings", "login", "apps"):
            reg.add("owner", owner, ())
            if m.group(2) and len(m.group(2)) >= 4 and m.group(2).lower() not in STOP:
                reg.add("project", re.sub(r"\.git$", "", m.group(2)), ())
    for m in LABEL_RX.finditer(text):
        v = _clean(m.group("v"))
        v = re.split(r"\s{2,}|\t|\bdate\b|\bsign|\bscale\b|\bsheet\b|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", v, flags=re.I)[0].strip()
        if not v:
            continue
        if m.group("p"):
            if NAME_LIKE.match(v) and _ok(v, 2):
                reg.add("person", v, ())
        elif m.group("o"):
            if _ok(v) and len(WORD.findall(v)) <= 6:
                reg.add("org", v, ())
    for m in INLINE_BY.finditer(text):
        reg.add("person", m.group(1), ())


def build_from_scan(scan_path: str) -> Registry:
    reg = Registry()
    with gzip.open(scan_path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            k, r = rec.get("k"), rec.get("r") or {}
            if k == "pdf":
                meta = r.get("meta") or {}
                if meta.get("author"):
                    reg.add_account(meta["author"])
                xmp = r.get("xmp") or ""
                for m in re.finditer(r"<dc:creator>.*?</dc:creator>", xmp, re.S):
                    for li in re.findall(r"<rdf:li[^>]*>([^<]{2,80})</rdf:li>", m.group(0)):
                        reg.add_account(li)
                for a in r.get("annots") or []:
                    t = a.split(" | ")[0]
                    if t and NAME_LIKE.match(t.strip()):
                        reg.add("person", t, ())
                _from_text(reg, (r.get("text") or "")[:200000])
                _from_text(reg, xmp)
            elif k == "ole":
                p = r.get("props") or {}
                for f_ in ("author", "last_saved_by", "manager"):
                    if p.get(f_):
                        reg.add_account(p[f_])
                if p.get("company"):
                    reg.add("org", p["company"], ())
            elif k == "step":
                fn = r.get("fn") or []
                if len(fn) >= 7:
                    for v in [fn[2], fn[-1]]:
                        v = v.replace("''", "'")
                        if v.strip():
                            reg.add_account(v)
                    if fn[3].strip():
                        reg.add("org", fn[3].replace("''", "'"), ())
    return reg
