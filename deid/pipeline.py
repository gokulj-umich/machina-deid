"""Run the de-identification pipeline over the whole corpus.

    python3 -m deid.pipeline --input ~/work/input --out ~/work/attempt-1 \
        --private ~/work/private --workers 8

Writes <out>/corpus/ (cleaned files), <out>/README.txt and <out>/code/.
Private material (secret key, path mapping, run log) goes to --private, which
must stay outside the submission folder.
"""
from __future__ import annotations

import argparse
import gc
import json
import multiprocessing as mp
import os
import shutil
import sys
import time

from . import identities
from .handlers import Ctx, process
from .layout import Layout

BIG = 300 * 1024 * 1024
LAYOUT: Layout | None = None
OUT = ""
IN = ""


def list_files(root: str) -> list[str]:
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not (dp == root and d == ".get-input")]
        for fn in fns:
            full = os.path.join(dp, fn)
            if os.path.islink(full):
                continue
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


def _work(item):
    rel, out_rel = item
    notes: list = []
    t = time.time()
    src = os.path.join(IN, rel)
    dst = os.path.join(OUT, out_rel)
    try:
        with open(src, "rb") as f:
            data = f.read()
        ctx = Ctx(LAYOUT, notes)
        repo_ctx = None
        parts = rel.split("/")
        if parts[0] == "archives" and len(parts) == 2:
            repo_ctx = LAYOUT.repos.get(os.path.splitext(parts[1])[0])
        elif len(parts) >= 3 and parts[0] == "sources" and parts[1] in ("github", "gitlab"):
            repo_ctx = LAYOUT.repos.get(parts[2])
        new = process(data, rel, ctx, repo_ctx) if data else data
        if not data:
            notes.append((rel, "source file is empty; kept as an empty file"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(new)
        return rel, out_rel, len(data), new != data, notes, time.time() - t, None
    except Exception as e:  # never drop a file silently
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
        except Exception:
            pass
        notes.append((rel, f"pipeline error ({type(e).__name__}); file copied without changes"))
        return rel, out_rel, 0, False, notes, time.time() - t, repr(e)


def main(argv=None):
    global LAYOUT, OUT, IN
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--private", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--limit", type=int, default=0, help="process only the first N files (testing)")
    ap.add_argument("--scan", default="", help="raw_scan.jsonl.gz from tools/discover.py (identity registry)")
    a = ap.parse_args(argv)

    IN = os.path.abspath(a.input)
    corpus = os.path.join(os.path.abspath(a.out), "corpus")
    OUT = corpus
    os.makedirs(corpus, exist_ok=True)
    os.makedirs(a.private, exist_ok=True)
    identities.load_secret(a.private)

    t0 = time.time()
    files = list_files(IN)
    # the layout dictionary is built from the full manifest when available so
    # partial/test runs use exactly the same pseudonyms as the full run
    man = os.path.join(IN, ".get-input", "manifest.tsv")
    names = files
    if os.path.exists(man):
        names = [l.split("\t", 1)[0] for l in open(man, encoding="utf-8") if l and not l.startswith("#")]
    extra = []
    if a.scan and os.path.exists(a.scan):
        from .registry import build_from_scan
        reg = build_from_scan(a.scan)
        reg.dump(os.path.join(a.private, "registry.tsv"))
        extra = reg.entries()
        print(f"[deid] registry: {len(reg.ids)} identities, {len(extra)} spellings from scan", flush=True)
    LAYOUT = Layout(names, extra)
    print(f"[deid] {len(files)} files, {len(LAYOUT.entries)} identity spellings", flush=True)
    if a.limit:
        files = files[: a.limit]

    plan, seen = [], {}
    for rel in files:
        o = LAYOUT.map_path(rel)
        if o in seen:  # two inputs collapse to one output name
            stem, ext = os.path.splitext(o)
            i = 2
            while f"{stem}~{i}{ext}" in seen:
                i += 1
            o = f"{stem}~{i}{ext}"
        seen[o] = rel
        plan.append((rel, o))

    sizes = {rel: os.path.getsize(os.path.join(IN, rel)) for rel, _ in plan}
    small = [p for p in plan if sizes[p[0]] < BIG]
    big = [p for p in plan if sizes[p[0]] >= BIG]
    small.sort(key=lambda p: -sizes[p[0]])

    all_notes, changed, done, errors = [], 0, 0, 0
    log = open(os.path.join(a.private, "run-log.jsonl"), "w")

    def record(r):
        nonlocal changed, done, errors
        rel, o, n, ch, notes, dt, err = r
        done += 1
        changed += bool(ch)
        errors += bool(err)
        all_notes.extend((LAYOUT.map_path(p) if p == rel else o + " :: " + LAYOUT.map_member(p, None), m) for p, m in notes)
        log.write(json.dumps({"in": rel, "out": o, "bytes": n, "changed": ch, "sec": round(dt, 3), "err": err}) + "\n")
        if done % 2000 == 0:
            print(f"[deid] {done}/{len(plan)} files, {changed} changed, {errors} errors, {time.time()-t0:.0f}s", flush=True)

    ctx = mp.get_context("fork")
    with ctx.Pool(a.workers, maxtasksperchild=500) as pool:
        for r in pool.imap_unordered(_work, small, chunksize=4):
            record(r)
    for p in big:  # one at a time to bound memory
        record(_work(p))
        gc.collect()
    log.close()

    with open(os.path.join(a.private, "path-map.tsv"), "w", encoding="utf-8", errors="replace") as f:
        for rel, o in plan:
            f.write(f"{rel}\t{o}\n")

    write_readme(os.path.abspath(a.out), all_notes, len(plan), changed, errors)
    copy_code(os.path.abspath(a.out))
    print(f"[deid] done: {done} files, {changed} changed, {errors} errors in {time.time()-t0:.0f}s", flush=True)


def write_readme(out_dir, notes, n, changed, errors):
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "README_TEMPLATE.txt"), encoding="utf-8") as f:
        tpl = f.read()
    lines = [f"- {p}: {m}" for p, m in sorted(set(notes))]
    body = tpl.replace("{{STATS}}", f"{n} files processed, {changed} modified, {errors} pipeline errors.")
    body = body.replace("{{NOTES}}", "\n".join(lines) if lines else "- none")
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8", errors="replace") as f:
        f.write(body)


def copy_code(out_dir):
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    dst = os.path.join(out_dir, "code")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(repo, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "private", "attempt-*", "input"))


if __name__ == "__main__":
    sys.exit(main())
