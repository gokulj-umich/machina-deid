"""Corpus-wide identity discovery (read-only; writes reports only).

    python3 tools/discover.py --input ~/work/input --out ~/work/reports --workers 8

Scans every PDF (text as rendered, metadata, XMP, annotations, form fields,
attachments), every OLE compound file (SummaryInformation and
DocumentSummaryInformation, plus UTF-16/ASCII strings in property streams) and
every STEP header, then aggregates candidate identities by how many files and
how many *unrelated sources* they appear in. Identities that were planted across
the corpus show up as strings shared by many unrelated sources.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import multiprocessing as mp
import os
import re
import sys
import time

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,24}")
URL = re.compile(r"(?i)\b(?:https?://|www\.)[A-Za-z0-9\-._~%/?#=&+:]+")
PHONE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{2,4}\)[\s.\-]?)?\d{2,4}[\s.\-]\d{3,4}[\s.\-]\d{3,4}(?![\w.])")
COPYR = re.compile(r"(?i)(?:©|\(c\)|copyright)\s*[^\n]{0,80}")
LABEL = re.compile(
    r"(?im)\b(drawn|drawn by|designed|designed by|design|checked|checked by|approved|approved by|engineer|author|"
    r"owner|company|customer|client|project|prepared by|created by|modified by|revised by|dwg\.? ?no\.?|drawing no\.?|"
    r"part no\.?|part number|p/n|document no\.?|doc\.? ?no\.?|job no\.?|contract|supplier|vendor|manufacturer|address|"
    r"phone|tel\.?|fax|e-?mail|website|web|dept\.?|department|division|site|location|cage code|cage)\s*[:#.\-]?\s*([^\n]{1,60})"
)
CAPS = re.compile(r"\b(?:[A-Z][A-Za-z0-9&'\-]+|[A-Z]{2,}[0-9]*)(?:\s+(?:[A-Z][A-Za-z0-9&'\-]+|[A-Z]{2,}[0-9]*|&|of|de|und|and)){0,4}")
ORG_SUFFIX = re.compile(r"(?i)\b([A-Z][\w&\-.' ]{1,40}?\s(?:Inc|Incorporated|LLC|L\.L\.C|Ltd|Limited|GmbH|AG|SA|S\.A|SAS|SRL|S\.r\.l|BV|B\.V|Corp|Corporation|Co|Company|Labs?|Laboratory|Laboratories|Industries|Engineering|Technologies|Technology|Systems|Robotics|University|Institute|Foundation|Group|Holdings|Associates|Works|Manufacturing|Solutions|Design|Studio|Lab)\b\.?)")


def source_of(rel: str) -> str:
    c = rel.split("/")
    if c[0] == "archives":
        return "archives/" + c[1]
    if len(c) >= 3 and c[0] == "sources":
        if c[1] == "existing":
            return "/".join(c[:5])
        return "/".join(c[:3])
    return c[0]


def scan_pdf(path: str) -> dict:
    import pymupdf
    out = {"meta": {}, "xmp": "", "text": "", "annots": [], "widgets": [], "files": [], "toc": [], "err": None, "pages": 0}
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        out["err"] = f"open:{type(e).__name__}"
        return out
    try:
        out["pages"] = doc.page_count
        out["meta"] = {k: v for k, v in (doc.metadata or {}).items() if v}
        try:
            out["xmp"] = (doc.get_xml_metadata() or "")[:20000]
        except Exception:
            pass
        texts = []
        for i, page in enumerate(doc):
            if i >= 60:
                break
            try:
                texts.append(page.get_text("text"))
            except Exception:
                pass
            try:
                for a in page.annots() or []:
                    info = a.info or {}
                    s = " | ".join(str(v) for v in (info.get("title"), info.get("content"), info.get("subject")) if v)
                    if s:
                        out["annots"].append(s[:300])
                for w in page.widgets() or []:
                    if w.field_value:
                        out["widgets"].append(f"{w.field_name}={w.field_value}"[:300])
            except Exception:
                pass
        out["text"] = "\n".join(texts)[:200000]
        try:
            out["files"] = list(doc.embfile_names())
        except Exception:
            pass
        try:
            out["toc"] = [t[1] for t in doc.get_toc()][:200]
        except Exception:
            pass
    except Exception as e:
        out["err"] = f"scan:{type(e).__name__}"
    finally:
        doc.close()
    return out


def scan_ole(path: str) -> dict:
    import olefile
    out = {"props": {}, "streams": [], "strings": [], "err": None}
    try:
        ole = olefile.OleFileIO(path)
    except Exception as e:
        out["err"] = f"open:{type(e).__name__}"
        return out
    try:
        m = ole.get_metadata()
        for attr in olefile.OleMetadata.SUMMARY_ATTRIBS + olefile.OleMetadata.DOCSUM_ATTRIBS:
            v = getattr(m, attr, None)
            if isinstance(v, bytes):
                v = v.decode("latin-1", "replace")
            if v and isinstance(v, str) and v.strip():
                out["props"][attr] = v.strip()[:200]
        for entry in ole.listdir(streams=True, storages=False):
            name = "/".join(entry)
            out["streams"].append(name)
            low = name.lower()
            # property-set style streams carry names/accounts as plain strings
            if ("\x05" in name or "propert" in low or "summary" in low or "design tracking" in low
                    or "information" in low) and ole.get_size(entry) < 2_000_000:
                data = ole.openstream(entry).read()
                for m in re.finditer(rb"(?:[\x20-\x7e]\x00){4,80}", data):
                    out["strings"].append(m.group(0).decode("utf-16-le", "replace"))
                for m in re.finditer(rb"[\x20-\x7e]{5,80}", data):
                    out["strings"].append(m.group(0).decode("latin-1"))
        out["strings"] = list(dict.fromkeys(out["strings"]))[:300]
    except Exception as e:
        out["err"] = f"scan:{type(e).__name__}"
    finally:
        ole.close()
    return out


STEP_FN = re.compile(r"FILE_NAME\s*\((.*?)\)\s*;", re.S)
STEP_STR = re.compile(r"'((?:[^']|'')*)'")


def scan_step(path: str) -> dict:
    with open(path, "rb") as f:
        head = f.read(20000).decode("latin-1")
    m = STEP_FN.search(head)
    return {"fn": STEP_STR.findall(m.group(1)) if m else []}


def work(item):
    root, rel = item
    p = os.path.join(root, rel)
    try:
        with open(p, "rb") as f:
            magic = f.read(8)
    except Exception:
        return rel, None, None
    low = rel.lower()
    try:
        if magic.startswith(b"%PDF") or low.endswith(".pdf"):
            return rel, "pdf", scan_pdf(p)
        if magic.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            return rel, "ole", scan_ole(p)
        if magic.startswith(b"ISO-1030") or low.endswith((".step", ".stp")):
            return rel, "step", scan_step(p)
    except Exception as e:
        return rel, "err", {"err": repr(e)}
    return rel, None, None


class Agg:
    def __init__(self):
        self.c = collections.defaultdict(lambda: [0, set(), []])  # value -> [files, sources, examples]

    def add(self, val, rel):
        val = re.sub(r"\s+", " ", val).strip()
        if not val or len(val) < 2:
            return
        e = self.c[val]
        e[0] += 1
        e[1].add(source_of(rel))
        if len(e[2]) < 3:
            e[2].append(rel)

    def dump(self, path, title, min_files=1, limit=4000, sort_sources=True):
        rows = [(v, e[0], len(e[1]), e[2]) for v, e in self.c.items() if e[0] >= min_files]
        rows.sort(key=lambda r: (-(r[2] if sort_sources else r[1]), -r[1], r[0]))
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n# columns: n_sources\tn_files\tvalue\texample_paths\n")
            for v, n, s, ex in rows[:limit]:
                f.write(f"{s}\t{n}\t{v}\t{' || '.join(ex)}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()
    root = os.path.abspath(a.input)
    os.makedirs(a.out, exist_ok=True)
    files = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not (dp == root and d == ".get-input")]
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/")
            low = rel.lower()
            if low.endswith((".pdf", ".step", ".stp", ".ipt", ".iam", ".idw", ".ipn", ".sldprt", ".sldasm", ".slddrw",
                             ".par", ".asm", ".psm", ".dft", ".doc", ".xls", ".ppt", ".msg", ".dwg", ".catpart", ".prt")):
                files.append(rel)
    print(f"[discover] {len(files)} candidate files", flush=True)

    meta = collections.defaultdict(Agg)
    pats = collections.defaultdict(Agg)
    ole_props = collections.defaultdict(Agg)
    ole_strings = Agg()
    step = collections.defaultdict(Agg)
    caps = Agg()
    counts = collections.Counter()
    t0 = time.time()
    raw = gzip.open(os.path.join(a.out, "raw_scan.jsonl.gz"), "wt", encoding="utf-8")
    with mp.get_context("fork").Pool(a.workers, maxtasksperchild=200) as pool:
        for i, (rel, kind, r) in enumerate(pool.imap_unordered(work, [(root, f) for f in files], chunksize=8)):
            if i % 5000 == 0:
                print(f"[discover] {i}/{len(files)} {time.time()-t0:.0f}s", flush=True)
            if not kind or r is None:
                continue
            counts[kind] += 1
            if r.get("err"):
                counts[f"{kind}_err"] += 1
            raw.write(json.dumps({"p": rel, "k": kind, "r": r}, ensure_ascii=False) + "\n")
            if kind == "pdf":
                for k, v in r["meta"].items():
                    if k not in ("format", "encryption", "creationDate", "modDate", "trapped"):
                        meta[k].add(str(v), rel)
                blob = "\n".join([r["text"], " ".join(r["annots"]), " ".join(r["widgets"]), " ".join(r["toc"]),
                                  " ".join(r["files"]), r["xmp"]])
                seen = set()
                for name, rx in (("email", EMAIL), ("url", URL), ("phone", PHONE), ("copyright", COPYR), ("orgsuffix", ORG_SUFFIX)):
                    for m in rx.finditer(blob):
                        v = m.group(1) if rx is ORG_SUFFIX else m.group(0)
                        if (name, v) not in seen:
                            seen.add((name, v))
                            pats[name].add(v, rel)
                for m in LABEL.finditer(r["text"]):
                    v = f"{m.group(1).lower()}: {m.group(2).strip()}"
                    if ("label", v) not in seen:
                        seen.add(("label", v))
                        pats["label"].add(v, rel)
                for v in set(x.strip() for x in CAPS.findall(r["text"][:50000])):
                    if len(v) >= 4:
                        caps.add(v, rel)
                for s in r["annots"]:
                    pats["annot"].add(s, rel)
                for s in r["widgets"]:
                    pats["widget"].add(s, rel)
                for s in r["files"]:
                    pats["attachment"].add(s, rel)
                for m in re.finditer(r"<(dc:creator|xmp:CreatorTool|pdf:Producer|pdf:Author|photoshop:AuthorsPosition|xmpRights:Owner|dc:rights|pdfx:[A-Za-z]+|xmp:Author|xmpMM:[A-Za-z]+|stEvt:softwareAgent)[^>]*>(.*?)</\1>", r["xmp"], re.S):
                    v = re.sub(r"<[^>]+>", " ", m.group(2))
                    pats["xmp:" + m.group(1)].add(v, rel)
            elif kind == "ole":
                for k, v in r["props"].items():
                    ole_props[k].add(v, rel)
                for s in r["strings"]:
                    ole_strings.add(s, rel)
                pats["ole_streams"].add(" ; ".join(sorted(set(re.sub(r"[\x00-\x1f]", "#", x) for x in r["streams"])))[:300], rel)
            elif kind == "step":
                fn = r["fn"]
                if len(fn) >= 7:
                    step["author"].add(fn[2], rel)
                    step["organization"].add(fn[3], rel)
                    step["authorisation"].add(fn[-1], rel)
                    step["originating_system"].add(fn[-2], rel)
    raw.close()
    R = a.out
    for k, ag in meta.items():
        ag.dump(f"{R}/pdf_meta_{k}.tsv", f"PDF metadata field {k}")
    for k, ag in pats.items():
        ag.dump(f"{R}/pdf_{k.replace(':', '_')}.tsv", f"PDF pattern {k}")
    for k, ag in ole_props.items():
        ag.dump(f"{R}/ole_prop_{k}.tsv", f"OLE property {k}")
    ole_strings.dump(f"{R}/ole_strings.tsv", "strings in OLE property streams", min_files=2)
    for k, ag in step.items():
        ag.dump(f"{R}/step_{k}.tsv", f"STEP FILE_NAME {k}")
    caps.dump(f"{R}/pdf_capitalized_phrases.tsv", "capitalized phrases in PDF text", min_files=3, limit=8000)
    with open(f"{R}/summary.txt", "w") as f:
        f.write(json.dumps(counts, indent=1) + f"\nseconds: {time.time()-t0:.0f}\n")
    print(f"[discover] done {dict(counts)} in {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    sys.exit(main())
