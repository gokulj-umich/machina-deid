#!/usr/bin/env bash
# Full attempt on the server: discovery scan (reused if present) -> pipeline
# with the identity registry -> self-check -> publish reports to the bucket.
#   bash tools/attempt.sh N
set -uo pipefail
N="${1:?attempt number}"
WORK="${WORK:-$HOME/work}"; HERE="$(cd "$(dirname "$0")/.." && pwd)"
BUCKET="${BUCKET:-gs://gocool-reports-50587c90}"
mkdir -p "$WORK/reports" "$WORK/private"
exec > >(tee -a "$WORK/reports/attempt-$N-console.log") 2>&1
echo "[attempt] $N start $(date)"
"$WORK/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
bash "$HERE/tools/discover.sh"
OUT="$WORK/attempt-$N"
if [ -e "$OUT" ]; then echo "[attempt] $OUT already exists; use a new number"; exit 1; fi
cd "$HERE"
nice "$WORK/.venv/bin/python" -W ignore -m deid.pipeline --input "$WORK/input" --out "$OUT" --private "$WORK/private" \
  --workers "$(nproc)" --scan "$WORK/reports/r1/raw_scan.jsonl.gz" 2>&1 | grep -E '^\[deid\]' | tee "$WORK/private/attempt-$N.log"
CHK="$WORK/reports/check-$N"
"$WORK/.venv/bin/python" -W ignore "$HERE/tools/selfcheck.py" --input "$WORK/input" --attempt "$OUT" \
  --private "$WORK/private" --out "$CHK" --workers "$(nproc)" > /dev/null 2>&1
cp "$WORK/private/registry.tsv" "$CHK/registry.tsv" 2>/dev/null
cp "$OUT/README.txt" "$CHK/README.txt" 2>/dev/null
cp "$WORK/private/attempt-$N.log" "$CHK/pipeline.log" 2>/dev/null
gcloud storage cp -r "$CHK" "$BUCKET/" --quiet && echo "[attempt] reports published to $BUCKET/check-$N/"
echo "[attempt] DONE attempt-$N $(date)"
