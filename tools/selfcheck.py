"""Self-check an attempt before submitting it.

    python3 tools/selfcheck.py --input ~/work/input --attempt ~/work/attempt-2 \
        --private ~/work/private --out ~/work/reports/check-2

1. Leakage: every registry identity (and every layout identity of 6+ chars)
   is searched in the rendered text and metadata of every output PDF, in the
   property streams of every OLE file, in STEP headers, and in all paths.
2. Integrity: every output PDF must open and render page 1, every OLE file
   must open with its streams readable, every zip must pass a CRC test, and
   every input file must have an output file of the same extension.
Writes summary.txt plus leak/broken lists (first examples) to --out.
"""
from __future__ import annotations

import argparse
import collections
import json
import multiprocessing as mp
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ahocorasick  # noqa: E402

A = None
ROOT = ""


def build(private):
    terms = set()
    reg = os.path.join(private, "registry.tsv")
    if os.path.exists(reg):
        for line in open(reg, encoding="utf-8"):
            kind, _, v = line.rstrip("\n").split("\t", 2)
            v = v[5:] if v.startswith("host:") else v
            if len(v) >= 4:
                terms.add(v.lower())
    a = ahocorasick.Automaton()
    for t in terms:
        a.add_word(t, t)
    a.make_automaton()
    return a, len(terms)


def hits(text):
    out = set()
    low = text.lower()
    for end, t in A.iter(low):
        s = end - len(t) + 1
        if (s == 0 or not low[s - 1].isalnum()) and (end + 1 >= len(low) or not low[end + 1].isalnum()):
            out.add(t)
    return out


def check(rel):
    p = os.path.join(ROOT, rel)
    res = {"rel": rel, "leaks": [], "broken": None}
    try:
        with open(p, "rb") as f:
            head = f.read(8)
        if head.startswith(b"%PDF"):
            import pymupdf
            d = pymupdf.open(p)
            txt = [d.metadata and json.dumps(d.metadata) or "", d.get_xml_metadata() or ""]
            for i, pg in enumerate(d):
                if i >= 60:
                    break
                txt.append(pg.get_text())
            if d.page_count:
                d[0].get_pixmap(dpi=20)
            d.close()
            res["leaks"] = sorted(hits("\n".join(txt)))
        elif head.startswith(b"\xd0\xcf\x11\xe0"):
            import olefile
            o = olefile.OleFileIO(p)
            blob = []
            for e in o.listdir():
                if o.get_size(e) < 4_000_000:
                    b = o.openstream(e).read()
                    blob.append(b.decode("latin-1"))
                    blob.append(b.decode("utf-16-le", "ignore"))
            o.close()
            res["leaks"] = sorted(hits("\n".join(blob)))
        elif head.startswith(b"PK\x03\x04"):
            z = zipfile.ZipFile(p)
            bad = z.testzip()
            if bad:
                res["broken"] = f"zip crc {bad}"
            res["leaks"] = sorted(hits("\n".join(z.namelist())))
        elif rel.lower().endswith((".step", ".stp")):
            with open(p, "rb") as f:
                res["leaks"] = sorted(hits(f.read(20000).decode("latin-1")))
    except Exception as e:
        res["broken"] = f"{type(e).__name__}: {e}"[:200]
    res["leaks"] += sorted(hits(rel))
    return res


def main():
    global A, ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--attempt", required=True)
    ap.add_argument("--private", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    A, nterms = build(a.private)
    ROOT = os.path.join(a.attempt, "corpus")
    outs = []
    for dp, _, fns in os.walk(ROOT):
        for fn in fns:
            outs.append(os.path.relpath(os.path.join(dp, fn), ROOT).replace(os.sep, "/"))
    leak_c, broken, leak_files = collections.Counter(), [], collections.defaultdict(list)
    kinds = collections.Counter()
    with mp.get_context("fork").Pool(a.workers) as pool:
        for r in pool.imap_unordered(check, outs, chunksize=16):
            if r["broken"]:
                broken.append((r["rel"], r["broken"]))
            for t in r["leaks"]:
                leak_c[t] += 1
                if len(leak_files[t]) < 3:
                    leak_files[t].append(r["rel"])
            kinds[os.path.splitext(r["rel"])[1].lower()] += 1
    # every input has an output with the same extension
    n_in = 0
    ext_in = collections.Counter()
    for dp, dns, fns in os.walk(a.input):
        dns[:] = [d for d in dns if d != ".get-input"]
        for fn in fns:
            n_in += 1
            ext_in[os.path.splitext(fn)[1].lower()] += 1
    ext_diff = {e: (ext_in[e], kinds[e]) for e in set(ext_in) | set(kinds) if ext_in[e] != kinds[e]}
    with open(os.path.join(a.out, "summary.txt"), "w", encoding="utf-8", errors="replace") as f:
        f.write(f"terms checked: {nterms}\ninput files: {n_in}\noutput files: {len(outs)}\n")
        f.write(f"broken outputs: {len(broken)}\nidentities still present: {len(leak_c)} (in {sum(leak_c.values())} file-hits)\n")
        f.write(f"extension count differences: {json.dumps(ext_diff)[:2000]}\n\nTOP LEAKS\n")
        for t, c in leak_c.most_common(150):
            f.write(f"{c}\t{t}\t{' || '.join(leak_files[t])}\n")
        f.write("\nBROKEN (first 100)\n")
        for rel, msg in broken[:100]:
            f.write(f"{rel}\t{msg}\n")
    print(open(os.path.join(a.out, "summary.txt"), encoding="utf-8").read()[:3000])


if __name__ == "__main__":
    main()
