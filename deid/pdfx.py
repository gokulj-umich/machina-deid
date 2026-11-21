"""PDF de-identification at the level of rendered text (PyMuPDF).

Raw PDF bytes rarely contain names as plain text: drawings use subset fonts,
CID encodings and per-glyph positioning. So each page is read the way a viewer
renders it (characters with their boxes), identities are matched on that text
with word boundaries, exactly those glyphs are removed with a redaction that
leaves vector geometry and images untouched, and the pseudonym is written at
the same position, size and direction. Metadata, XMP, annotations, form
fields, bookmarks and attachments are rewritten with the same registry.
"""
from __future__ import annotations

import math
import re

import pymupdf

from .identities import pseudo
from .replace import apply_text

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)

GENERIC = {"", "unknown", "none", "n/a", "anonymous", "user", "admin", "administrator", "author", "owner", "default", "-"}
ACCOUNT_RE = re.compile(r"^\s*([A-Za-z0-9][\w.\-]{1,40})\\([\w.\- ]{1,40}?)(?:\s*\(([^)]*)\))?\s*$")


def account_pseudo(v: str) -> str:
    """Pseudonymise author/account strings, keeping their structure.

    'DESKTOP-Q0EMQN5\\ROPE (DESKTOP-Q0EMQN5)' -> 'HOST-XXXXX\\PersonYYYYY (HOST-XXXXX)'
    """
    s = v.strip()
    if s.lower() in GENERIC:
        return v
    m = ACCOUNT_RE.match(s)
    if m:
        host, user, extra = m.group(1), m.group(2), m.group(3)
        h = host_pseudo(host)
        u = pseudo("person", user)
        out = f"{h}\\{u}"
        if extra:
            out += f" ({host_pseudo(extra) if extra.strip().lower() == host.lower() else pseudo('person', extra)})"
        return out
    return pseudo("person", s)


def host_pseudo(host: str) -> str:
    return pseudo("org", "host:" + host.lower())


def _page_chars(page):
    """Return (text, boxes) where text[i] has bounding box boxes[i] (None for separators)."""
    chars, boxes = [], []
    d = page.get_text("rawdict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            ldir = line.get("dir", (1, 0))
            for span in line["spans"]:
                size = span.get("size", 10)
                for ch in span["chars"]:
                    chars.append(ch["c"])
                    boxes.append((ch["bbox"], ldir, size, ch.get("origin")))
            chars.append("\n")
            boxes.append(None)
    return "".join(chars), boxes


def _union(bxs):
    x0 = min(b[0] for b in bxs); y0 = min(b[1] for b in bxs)
    x1 = max(b[2] for b in bxs); y1 = max(b[3] for b in bxs)
    return pymupdf.Rect(x0, y0, x1, y1)


def _text_matches(text: str, rep):
    """Identity matches on unicode text: [(start, end, pseudonym)]."""
    b = text.encode("utf-8")
    s = b.decode("latin-1")
    low = s.lower()
    spans = rep.spans(s, low)
    out = []
    # convert byte offsets back to character offsets
    if not spans:
        return out
    # build byte->char index
    idx = []
    for ci, ch in enumerate(text):
        idx.extend([ci] * len(ch.encode("utf-8")))
    idx.append(len(text))
    spans.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    pos = -1
    for st, en, r, wide in spans:
        if wide or st < pos:
            continue
        out.append((idx[st], idx[en], r))
        pos = en
    return out


def _redact_page(page, rep, extra_rx=None) -> int:
    text, boxes = _page_chars(page)
    if not text.strip():
        return 0
    matches = _text_matches(text, rep)
    if extra_rx:
        for rx, fn in extra_rx:
            for m in rx.finditer(text):
                r = fn(m)
                if r:
                    matches.append((m.start(r[0]), m.end(r[0]), r[1]))
    if not matches:
        return 0
    matches.sort(key=lambda m: (m[0], -(m[1] - m[0])))
    placed = []
    pos = -1
    for st, en, r in matches:
        if st < pos:
            continue
        pos = en
        seg = [boxes[i] for i in range(st, en) if boxes[i] is not None]
        if not seg or any(boxes[i] is None for i in range(st, en)):
            continue  # match crosses a line break
        rect = _union([s[0] for s in seg])
        if rect.is_empty or rect.width <= 0:
            continue
        # shrink slightly so neighbouring glyphs are never caught
        h = rect.height
        core = pymupdf.Rect(rect.x0 + 0.1, rect.y0 + h * 0.15, rect.x1 - 0.1, rect.y1 - h * 0.15)
        if core.is_empty:
            core = rect
        page.add_redact_annot(core, fill=False)
        ldir, size, origin = seg[0][1], seg[0][2], seg[0][3]
        placed.append((rect, r, ldir, size, origin))
    if not placed:
        return 0
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    for rect, r, ldir, size, origin in placed:
        angle = math.degrees(math.atan2(-ldir[1], ldir[0]))
        rot = int(round(angle / 90.0)) * 90 % 360
        length = max(rect.width, rect.height) if rot in (90, 270) else rect.width
        fs = size if size and size > 0 else rect.height * 0.8
        w = pymupdf.get_text_length(r, fontname="helv", fontsize=fs)
        if w > length * 1.1 and w > 0:
            fs = max(fs * (length * 1.1) / w, 2.5)
        pt = pymupdf.Point(origin) if origin else pymupdf.Point(rect.x0, rect.y1)
        try:
            page.insert_text(pt, r, fontname="helv", fontsize=fs, rotate=rot, color=(0, 0, 0))
        except Exception:
            pass
    return len(placed)


def rewrite_pdf(data: bytes, rep, process_member=None, map_name=None, extra_rx=None) -> tuple[bytes, list]:
    notes = []
    doc = pymupdf.open(stream=data, filetype="pdf")
    if doc.needs_pass:
        raise ValueError("encrypted PDF")
    n = 0
    # document info
    md = dict(doc.metadata or {})
    newmd = {}
    for k in ("author", "title", "subject", "keywords", "creator", "producer"):
        v = md.get(k) or ""
        if k == "author":
            nv = account_pseudo(v) if v else v
        else:
            nv = apply_text(v, rep) if v else v
        newmd[k] = nv
        n += nv != v
    for k in ("creationDate", "modDate"):
        if md.get(k):
            newmd[k] = md[k]
    if n:
        doc.set_metadata(newmd)
    # XMP: rewrite strings in place with the same registry, author values included
    try:
        xmp = doc.get_xml_metadata() or ""
    except Exception:
        xmp = ""
    if xmp:
        nx = apply_text(xmp, rep)
        au = (md.get("author") or "").strip()
        if au and au.lower() not in GENERIC:
            nx = nx.replace(au, account_pseudo(au))
        if nx != xmp:
            doc.set_xml_metadata(nx)
            n += 1
    # pages
    for page in doc:
        try:
            n += _redact_page(page, rep, extra_rx)
        except Exception as e:
            notes.append(f"page {page.number + 1}: text rewrite failed ({type(e).__name__})")
        try:
            for a in page.annots() or []:
                info = a.info or {}
                changed = False
                for key in ("title", "content", "subject"):
                    v = info.get(key) or ""
                    nv = (account_pseudo(v) if key == "title" and v else apply_text(v, rep)) if v else v
                    if nv != v:
                        info[key] = nv
                        changed = True
                if changed:
                    a.set_info(info)
                    a.update()
                    n += 1
            for w in page.widgets() or []:
                v = w.field_value
                if isinstance(v, str) and v:
                    nv = apply_text(v, rep)
                    if nv != v:
                        w.field_value = nv
                        w.update()
                        n += 1
        except Exception:
            pass
    # bookmarks
    try:
        toc = doc.get_toc(simple=False)
        if toc:
            nt = [[t[0], apply_text(t[1], rep), t[2]] + t[3:] for t in toc]
            if any(a[1] != b[1] for a, b in zip(toc, nt)):
                doc.set_toc(nt)
                n += 1
    except Exception:
        pass
    # attachments
    try:
        for name in list(doc.embfile_names()):
            buf = doc.embfile_get(name)
            nb = process_member(buf, name) if process_member else buf
            nn = map_name(name) if map_name else name
            if nb != buf or nn != name:
                doc.embfile_upd(name, buffer=nb, filename=nn, ufilename=nn)
                n += 1
    except Exception:
        pass
    if not n:
        doc.close()
        return data, notes
    out = doc.tobytes(garbage=1, deflate=True)
    doc.close()
    return out, notes
