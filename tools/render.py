"""Render the first page of some PDFs to small PNGs for visual inspection.

    python3 tools/render.py --input ~/work/input --list paths.txt --out ~/work/reports/png
"""
import argparse
import hashlib
import os

import pymupdf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=60)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    idx = []
    for rel in open(a.list, encoding="utf-8").read().splitlines():
        rel = rel.strip()
        if not rel:
            continue
        try:
            doc = pymupdf.open(os.path.join(a.input, rel))
            pix = doc[0].get_pixmap(dpi=a.dpi)
            name = hashlib.sha1(rel.encode()).hexdigest()[:10] + ".png"
            pix.save(os.path.join(a.out, name))
            idx.append(f"<p>{rel}<br><img src='{name}' style='max-width:1400px;border:1px solid #888'></p>")
            doc.close()
        except Exception as e:
            idx.append(f"<p>{rel}: {e}</p>")
    with open(os.path.join(a.out, "index.html"), "w", encoding="utf-8") as f:
        f.write("<html><body>" + "\n".join(idx) + "</body></html>")


if __name__ == "__main__":
    main()
