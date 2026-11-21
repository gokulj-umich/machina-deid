"""OLE compound files (Inventor IPT/IAM/IDW, SolidWorks <=2014, Solid Edge,
legacy Office): rewrite identities inside property-set streams.

Property streams (SummaryInformation, DocumentSummaryInformation and the
application property sets such as Inventor's "Design Tracking Properties")
hold author, last-saved-by account, company, manager and similar fields as
plain ANSI or UTF-16 strings. The container's stream sizes cannot change
without re-allocating sectors, so each value is replaced in place with an
identity-specific pseudonym of the same length (see identities.fit), which is
the same in every file. Every other stream is scanned for the same values so
no copy of an account or name survives elsewhere in the container.
"""
from __future__ import annotations

import io

import olefile

from .identities import pseudo
from .replace import Replacer, apply

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
PERSON_FIELDS = ("author", "last_saved_by", "manager")
ORG_FIELDS = ("company",)
GENERIC = {"", "unknown", "none", "user", "admin", "administrator", "author", "owner", "default", "-", "windows user",
           "valued customer", "registered user", "autodesk", "solidworks", "microsoft"}


def _val(v):
    if isinstance(v, bytes):
        v = v.decode("latin-1", "replace")
    return v.strip() if isinstance(v, str) else ""


def file_identities(ole) -> list[tuple[str, str, str]]:
    """(spelling, kind, identity) for the account/author values of this file."""
    out = []
    try:
        m = ole.get_metadata()
    except Exception:
        return out
    for f in PERSON_FIELDS + ORG_FIELDS:
        v = _val(getattr(m, f, None))
        if v and v.lower() not in GENERIC and len(v) >= 2:
            kind = "org" if f in ORG_FIELDS else "person"
            out.append((v, kind, v))
            # 'DOMAIN\\user' accounts: the user part on its own too
            if "\\" in v:
                u = v.split("\\")[-1].strip()
                if len(u) >= 2:
                    out.append((u, "person", u))
    return out


def rewrite_ole(data: bytes, rep: Replacer) -> tuple[bytes, int]:
    try:
        bio = io.BytesIO(bytearray(data))
        ole = olefile.OleFileIO(bio, write_mode=True)
    except Exception:
        return apply(data, rep, fixed=True, binary=True)[0], 0
    count = 0
    try:
        local = file_identities(ole)
        local_rep = Replacer(local) if local else None
        for entry in ole.listdir(streams=True, storages=False):
            name = "/".join(entry)
            size = ole.get_size(entry)
            if size == 0 or size > 64 * 1024 * 1024:
                continue
            raw = ole.openstream(entry).read()
            prop = name.startswith("\x05") or "\x05" in name or "propert" in name.lower() or "information" in name.lower()
            new = raw
            if local_rep:
                # this file's own accounts: matched at any length, in every stream
                new, n1 = apply(new, local_rep, fixed=True, text_rules=False)
                count += n1
            if prop:
                new, n2 = apply(new, rep, fixed=True, text_rules=False)
            else:
                new, n2 = apply(new, rep, fixed=True, binary=True)
            count += n2
            if new != raw and len(new) == len(raw):
                ole.write_stream(entry, new)
        ole.close()
        out = bio.getvalue()
    except Exception:
        try:
            ole.close()
        except Exception:
            pass
        return apply(data, rep, fixed=True, binary=True)[0], 0
    # names outside streams (directory entries, unused sectors)
    out, n3 = apply(bytes(out), rep, fixed=True, binary=True)
    return out, count + n3
