#!/usr/bin/env bash
# Run corpus discovery on the server and publish the reports to the private bucket.
set -uo pipefail
WORK="${WORK:-$HOME/work}"; HERE="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$WORK/reports"; exec > >(tee -a "$WORK/reports/discover-console.log") 2>&1
echo "[discover] start $(date)"
BUCKET="${BUCKET:-gs://gocool-reports-50587c90}"
"$WORK/.venv/bin/pip" install -q pymupdf==1.26.5 olefile==0.47
OUT="$WORK/reports/r1"; mkdir -p "$OUT"
if [ ! -s "$OUT/raw_scan.jsonl.gz" ] || [ ! -s "$OUT/summary.txt" ]; then
  "$WORK/.venv/bin/python" "$HERE/tools/discover.py" --input "$WORK/input" --out "$OUT" --workers "$(nproc)" 2>&1 | grep -v Warning | tee "$OUT/run.log"
fi
# sample drawings to look at: PDFs with the most title-block labels
python3 - "$OUT" <<'PY' > "$OUT/render_list.txt"
import sys,collections
c=collections.Counter()
for line in open(sys.argv[1]+"/pdf_label.tsv",encoding="utf-8"):
    if line.startswith("#"): continue
    p=line.rstrip("\n").split("\t")
    for ex in p[3].split(" || "): c[ex]+=1
seen=set();out=[]
for path,_ in c.most_common(400):
    src="/".join(path.split("/")[:4])
    if src in seen: continue
    seen.add(src); out.append(path)
    if len(out)>=40: break
print("\n".join(out))
PY
"$WORK/.venv/bin/python" "$HERE/tools/render.py" --input "$WORK/input" --list "$OUT/render_list.txt" --out "$OUT/png" || true
if gcloud storage cp -r "$OUT"/*.tsv "$OUT"/*.txt "$OUT"/*.log "$OUT/png" "$BUCKET/r1/" --quiet; then
  echo "[discover] PUBLISHED to $BUCKET/r1/"
else
  echo "[discover] UPLOAD FAILED (reports are in $OUT)"
fi
