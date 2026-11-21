"""Identity discovery and consistent pseudonyms.

Identities come from three places:
  1. the corpus folder layout (GitHub/GitLab owners and repos, archive names,
     web domains),
  2. a small seed list of organisations and people named in catalog folder
     titles and in the "existing" collections (seeds.py, built at run time
     from the manifest),
  3. values found inside files at run time (STEP author/organization fields,
     PDF/OLE author and company properties, e-mail addresses, user-profile
     paths). Those are pseudonymised on the fly with the same HMAC scheme, so
     every worker gives the same identity the same pseudonym without
     coordination.

Pseudonyms are deterministic: HMAC-SHA256(secret, kind + normalised identity).
The secret lives outside the submission (private/secret.key), so the mapping
cannot be reversed from the published code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

_SECRET: bytes | None = None


def load_secret(private_dir: str) -> bytes:
    global _SECRET
    os.makedirs(private_dir, exist_ok=True)
    p = os.path.join(private_dir, "secret.key")
    if not os.path.exists(p):
        with open(p, "wb") as f:
            f.write(os.urandom(32))
    with open(p, "rb") as f:
        _SECRET = f.read()
    return _SECRET


def set_secret(secret: bytes) -> None:
    global _SECRET
    _SECRET = secret


def norm(s: str) -> str:
    """Normalise an identity so case and separator variants collapse together."""
    s = s.casefold()
    s = re.sub(r"[\s_\-\.]+", "", s)
    return s


def code(kind: str, ident: str, n: int = 5) -> str:
    assert _SECRET is not None, "secret not loaded"
    d = hmac.new(_SECRET, f"{kind}\x00{norm(ident)}".encode("utf-8"), hashlib.sha256).digest()
    return base64.b32encode(d).decode("ascii")[:n]


# Readable, typed pseudonyms. No spaces, quotes or path separators, so they
# are safe inside file names, STEP strings, XML and CSV.
PREFIX = {
    "org": "Org",
    "person": "Person",
    "project": "Project",
    "owner": "Owner",
    "part": "Part",
}


def pseudo(kind: str, ident: str) -> str:
    if ident.startswith("host:"):
        return f"HOST-{code('org', ident)}"
    if kind == "domain":
        return f"site-{code('domain', ident).lower()}.example"
    if kind == "email":
        local, _, dom = ident.partition("@")
        return f"user-{code('email', ident).lower()}@site-{code('domain', dom).lower()}.example"
    return f"{PREFIX.get(kind, 'Id')}{code(kind, ident)}"


def fit(p: str, n: int) -> str:
    """Length-preserving form of a pseudonym (for binary / fixed-width formats).

    Shorter slots get a type letter plus identity-specific code characters, so
    two different identities never collapse to the same short string and the
    same identity always gets the same short form.
    """
    if n <= 0:
        return ""
    if len(p) <= n:
        return p + " " * (n - len(p))
    d = hashlib.sha256((_SECRET or b"") + b"\x01" + p.encode("utf-8")).digest()
    code32 = base64.b32encode(d).decode("ascii")
    return (p[0] + code32)[:n]


# ---------------------------------------------------------------------------
# Words that must never be treated as identities in file *contents*, even if
# an owner/repo happens to be named that way (they are ordinary engineering
# words or CAD software names that appear as format data).
STOP = set(
    """
    mechanical hardware resources parts part models model cad cam design designs
    files file docs documentation projects project assembly assemblies drawing
    drawings library libraries public private main master source sources common
    examples example test tests demo samples sample data dataset tools tool
    engine engines robot robots arm gripper machine machines printer cnc mill
    lathe vise gearbox hinge forge academy evo fab pro me lc s1 omi parts3d
    solidworks freecad autodesk inventor catia creo ptc siemens onshape fusion
    fusion360 step stp iges igs stl obj dxf dwg pdf svg nx parasolid acis
    open openhardware opensource hand head base frame plate bracket mount
    unknown none null default user users admin owner author company
    """.split()
)


def distinctive(word: str) -> bool:
    """Is this owner/repo/domain label specific enough to replace in contents?"""
    w = word.strip()
    if len(w) < 4:
        return False
    if norm(w) in STOP or w.casefold() in STOP:
        return False
    # purely numeric / hex-like labels are not identities
    if re.fullmatch(r"[0-9a-fA-F]+", w):
        return False
    return True


def variants(name: str) -> set[str]:
    """Spelling variants of an identifier as it may appear in text."""
    out = {name}
    parts = [p for p in re.split(r"[\s_\-\.]+", name) if p]
    if len(parts) > 1:
        out.add(" ".join(parts))
        out.add("-".join(parts))
        out.add("_".join(parts))
        out.add("".join(parts))
    # CamelCase -> "Camel Case"
    cc = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    if cc != name:
        out.add(cc)
    # leading digits (e.g. 2002Bishwajeet -> Bishwajeet)
    m = re.fullmatch(r"\d+([A-Za-z].{3,})", name)
    if m:
        out.add(m.group(1))
    return {v for v in out if len(v) >= 3}
