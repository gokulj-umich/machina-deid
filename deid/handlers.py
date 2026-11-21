"""Per-format de-identification. Every handler takes bytes and returns bytes.

Design rule: never re-encode geometry. Text formats get identity strings
replaced in place; binary and length-sensitive formats get length-preserving
replacements only; containers are opened, their members processed with the
same rules, and re-packed with the same member layout.
"""
from __future__ import annotations

import gzip
import io
import os
import re
import tarfile
import zipfile

from .identities import pseudo
from .replace import Replacer, apply, apply_text

FREE_TEXT = {
    "step", "stp", "p21", "ste", "ifc", "dxf", "svg", "wrl", "vrml", "x3d", "obj", "mtl",
    "txt", "md", "markdown", "csv", "tsv", "json", "xml", "html", "htm", "yml", "yaml",
    "ini", "cfg", "conf", "toml", "py", "js", "ts", "tsx", "jsx", "mjs", "css", "scad",
    "gcode", "nc", "ngc", "tap", "tex", "rst", "urdf", "sdf", "xacro", "launch", "ipynb",
    "kicad_pcb", "kicad_sch", "kicad_pro", "kicad_mod", "sch", "brd", "lbr", "fcmacro",
    "sh", "bat", "c", "h", "cpp", "hpp", "ino", "m", "rb", "go", "rs", "java", "lua",
    "license", "readme", "gitignore", "gitattributes", "dae", "gltf", "amf", "plt",
    "ps", "eps", "log", "bom", "lst", "rtf", "mjs", "lock", "properties", "inf",
}
FIXED_TEXT = {"igs", "iges", "sat", "sab", "x_t", "xmt_txt", "x_b", "xmt_bin", "unv", "neu", "emn", "emp", "stl"}
ZIP_KEEP_NAMES = {"fcstd", "3mf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "kmz", "epub", "f3d", "f3z", "fcbak", "jar", "apk", "xpi", "vsdx", "nupkg"}
ARCHIVE_ZIP = {"zip"}
GENERIC_VALUES = {
    "", "unknown", "none", "n/a", "na", "-", "--", "?", "undefined", "not specified", "unspecified",
    "nobody", "author", "organization", "organisation", "company", "user", "default", "anonymous",
    "null", "nil", "no", "not available", "unknown author", "unknown organization",
}

STEP_STR = re.compile(r"'((?:[^']|'')*)'")


def ext_of(name: str) -> str:
    n = name.lower()
    for dbl in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if n.endswith(dbl):
            return dbl[1:]
    e = os.path.splitext(n)[1][1:]
    if e.isdigit():  # Creo versioned files: part.prt.3
        e2 = os.path.splitext(os.path.splitext(n)[0])[1][1:]
        return e2 or e
    if not e:
        base = os.path.basename(n)
        return base  # README, LICENSE
    return e


class Ctx:
    def __init__(self, layout, notes):
        self.layout = layout
        self.rep: Replacer = layout.rep
        self.notes = notes  # list to append (path, message)


# ---------------------------------------------------------------------------
def _step_value(v: str, kind: str) -> str:
    if v.strip().lower() in GENERIC_VALUES:
        return v
    return pseudo(kind, v.strip())


def _rewrite_strings(arg_text: str, fn) -> str:
    return STEP_STR.sub(lambda m: "'" + fn(m.group(1).replace("''", "'")).replace("'", "''") + "'", arg_text)


def _split_top(args: str) -> list[str]:
    out, depth, cur, i, inq = [], 0, [], 0, False
    while i < len(args):
        ch = args[i]
        if inq:
            cur.append(ch)
            if ch == "'":
                if i + 1 < len(args) and args[i + 1] == "'":
                    cur.append("'")
                    i += 1
                else:
                    inq = False
        elif ch == "'":
            inq = True
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        elif ch == "/" and i + 1 < len(args) and args[i + 1] == "*":
            j = args.find("*/", i + 2)
            j = len(args) if j < 0 else j + 2
            cur.append(args[i:j])
            i = j - 1
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def _find_call(s: str, kw: str, start: int = 0, limit: int | None = None):
    """Find KW ( ... ) honouring quotes; returns (args_start, args_end) or None."""
    m = re.compile(kw + r"\s*\(", re.I).search(s, start, limit if limit else len(s))
    if not m:
        return None
    i, depth, inq = m.end(), 1, False
    while i < len(s):
        ch = s[i]
        if inq:
            if ch == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    i += 1
                else:
                    inq = False
        elif ch == "'":
            inq = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return m.end(), i
        i += 1
    return None


def step_header(data: bytes, ctx: Ctx) -> bytes:
    s = data.decode("latin-1")
    hdr_end = s.find("ENDSEC", 0, 200000)
    if hdr_end < 0:
        return data
    call = _find_call(s, "FILE_NAME", 0, hdr_end)
    if not call:
        return data
    a, b = call
    args = _split_top(s[a:b])
    if len(args) >= 7:
        def name_fn(v):
            v2 = re.split(r"[\\/]", v)[-1] if ("\\" in v or "/" in v) else v
            return apply_text(v2, ctx.rep)
        args[0] = _rewrite_strings(args[0], name_fn)
        args[2] = _rewrite_strings(args[2], lambda v: _step_value(v, "person"))
        args[3] = _rewrite_strings(args[3], lambda v: _step_value(v, "org"))
        args[6] = _rewrite_strings(args[6], lambda v: _step_value(v, "person"))
        s = s[:a] + ",".join(args) + s[b:]
    # AP203 PERSON / ORGANIZATION entities in the DATA section
    out, pos = [], 0
    for m in re.finditer(r"=\s*(PERSON|ORGANIZATION|PERSONAL_ADDRESS|ORGANIZATIONAL_ADDRESS)\s*\(", s):
        call = _find_call(s, re.escape(m.group(1)), m.start(), m.end() + 1)
        if not call:
            continue
        a, b = call
        if a < pos:
            continue
        kind = "person" if m.group(1).startswith("PERSON") else "org"
        if "ADDRESS" in m.group(1):
            new = _rewrite_strings(s[a:b], lambda v: v if v.strip().lower() in GENERIC_VALUES else f"Address{pseudo('org', v)[3:]}")
        else:
            whole = " ".join(x.replace("''", "'") for x in STEP_STR.findall(s[a:b]) if x.strip().lower() not in GENERIC_VALUES)
            new = _rewrite_strings(s[a:b], lambda v: v if v.strip().lower() in GENERIC_VALUES else pseudo(kind, whole or v))
        out.append(s[pos:a])
        out.append(new)
        pos = b
    if out:
        out.append(s[pos:])
        s = "".join(out)
    return s.encode("latin-1")


# ---------------------------------------------------------------------------
def pdf_handler(data: bytes, name: str, ctx: Ctx) -> bytes:
    from .pdfx import rewrite_pdf
    from .replace import EMAIL_RE
    extra = [(EMAIL_RE, lambda m: (0, pseudo("email", m.group(0))))]
    try:
        out, notes = rewrite_pdf(
            data, ctx.rep,
            process_member=lambda b, n: process(b, n, ctx),
            map_name=lambda n: ctx.layout.map_member(n, None),
            extra_rx=extra,
        )
        for msg in notes:
            ctx.notes.append((name, msg))
    except Exception as e:
        ctx.notes.append((name, f"PDF could not be parsed ({type(e).__name__}); identities replaced in raw bytes only"))
        out = data
    # names left in raw bytes (object names, uncompressed metadata copies)
    return apply(out, ctx.rep, fixed=True, binary=True)[0]


def _content_streams(page, pikepdf):
    seen = set()
    c = page.obj.get("/Contents")
    items = list(c) if isinstance(c, pikepdf.Array) else ([c] if c is not None else [])
    res = page.obj.get("/Resources")
    if res is not None and "/XObject" in res:
        for _, xo in res["/XObject"].items():
            try:
                if xo.get("/Subtype") == "/Form":
                    items.append(xo)
            except Exception:
                pass
    for it in items:
        try:
            og = it.objgen
        except Exception:
            og = id(it)
        if og in seen:
            continue
        seen.add(og)
        if isinstance(it, pikepdf.Stream):
            yield it


# ---------------------------------------------------------------------------
def zip_handler(data: bytes, name: str, ctx: Ctx, rename_members: bool, repo_ctx=None) -> bytes:
    try:
        zin = zipfile.ZipFile(io.BytesIO(data))
        infos = zin.infolist()
    except Exception as e:
        ctx.notes.append((name, f"archive could not be opened ({type(e).__name__}); identities replaced in raw bytes only"))
        return apply(data, ctx.rep, fixed=True, binary=True)[0]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", allowZip64=True) as zout:
        for info in infos:
            try:
                content = zin.read(info)
            except Exception as e:
                ctx.notes.append((name, f"member could not be read ({type(e).__name__}); kept as stored"))
                continue
            newname = info.filename
            if rename_members:
                newname = ctx.layout.map_member(info.filename, repo_ctx)
            if not info.is_dir():
                content = process(content, info.filename, ctx)
            ni = zipfile.ZipInfo(newname, date_time=info.date_time)
            ni.compress_type = info.compress_type if info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) else zipfile.ZIP_DEFLATED
            ni.external_attr = info.external_attr
            ni.create_system = info.create_system
            zout.writestr(ni, content)
    return out.getvalue()


def tar_handler(data: bytes, name: str, ctx: Ctx, repo_ctx=None) -> bytes:
    try:
        tin = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        members = tin.getmembers()
    except Exception as e:
        ctx.notes.append((name, f"tar could not be opened ({type(e).__name__})"))
        return apply(data, ctx.rep, fixed=True, binary=True)[0]
    lname = name.lower()
    mode = "w:gz" if lname.endswith((".gz", ".tgz")) else ("w:bz2" if lname.endswith(".bz2") else ("w:xz" if lname.endswith(".xz") else "w"))
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode=mode) as tout:
        for m in members:
            nm = tarfile.TarInfo(ctx.layout.map_member(m.name, repo_ctx))
            nm.mode, nm.mtime, nm.type = m.mode, m.mtime, m.type
            if m.isfile():
                content = process(tin.extractfile(m).read(), m.name, ctx)
                nm.size = len(content)
                tout.addfile(nm, io.BytesIO(content))
            elif m.issym() or m.islnk():
                nm.linkname = ctx.layout.map_member(m.linkname, repo_ctx)
                tout.addfile(nm)
            else:
                tout.addfile(nm)
    return out.getvalue()


def sevenz_handler(data: bytes, name: str, ctx: Ctx) -> bytes:
    try:
        import py7zr
        with py7zr.SevenZipFile(io.BytesIO(data), "r") as z:
            files = z.readall() or {}
    except Exception as e:
        ctx.notes.append((name, f"7z could not be opened ({type(e).__name__}); identities replaced in raw bytes only"))
        return apply(data, ctx.rep, fixed=True, binary=True)[0]
    out = io.BytesIO()
    with py7zr.SevenZipFile(out, "w") as z:
        for mname, bio in sorted(files.items()):
            content = process(bio.read(), mname, ctx)
            z.writestr(content, ctx.layout.map_member(mname, None))
    return out.getvalue()


def _looks_text(data: bytes) -> bool:
    head = data[:65536]
    return b"\x00" not in head and sum(b < 9 or 13 < b < 32 for b in head) < len(head) // 200 + 1


# ---------------------------------------------------------------------------
def process(data: bytes, name: str, ctx: Ctx, repo_ctx=None) -> bytes:
    """De-identify one file's bytes. name is used only to pick the handler."""
    e = ext_of(name)
    head = data[:8]
    try:
        if e in ("tar", "tgz", "tar.gz", "tar.bz2", "tar.xz"):
            return tar_handler(data, name, ctx, repo_ctx)
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            if e in ARCHIVE_ZIP:
                return zip_handler(data, name, ctx, rename_members=True, repo_ctx=repo_ctx)
            return zip_handler(data, name, ctx, rename_members=False)
        if e == "7z" and head.startswith(b"7z\xbc\xaf\x27\x1c"):
            return sevenz_handler(data, name, ctx)
        if e == "gz" and head.startswith(b"\x1f\x8b"):
            inner = gzip.decompress(data)
            return gzip.compress(process(inner, name[:-3], ctx), mtime=0)
        if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            from .ole import rewrite_ole
            return rewrite_ole(data, ctx.rep)[0]
        if e == "pdf" or head.startswith(b"%PDF"):
            return pdf_handler(data, name, ctx)
        if e in ("step", "stp", "p21", "ste") or head.startswith(b"ISO-10303"):
            data = step_header(data, ctx)
            return apply(data, ctx.rep, fixed=False)[0]
        if e == "stl" and not data[:5].lower() == b"solid":
            # binary STL: only the 80-byte header is text
            h, _ = apply(data[:80], ctx.rep, fixed=True, text_rules=False)
            return h + data[80:]
        if e in FREE_TEXT:
            return apply(data, ctx.rep, fixed=False)[0]
        # length-sensitive text and every binary/native format
        return apply(data, ctx.rep, fixed=True, binary=True)[0]
    except Exception as ex:
        ctx.notes.append((name, f"handler error ({type(ex).__name__}: {ex}); identities replaced in raw bytes only"))
        return apply(data, ctx.rep, fixed=True, binary=True)[0]
