#!/usr/bin/env bash
# Usage: bash run.sh N        (N = attempt number)
# Builds ~/work/attempt-N/{corpus,code,README.txt} from ~/work/input.
set -euo pipefail
N="${1:?attempt number}"
WORK="${WORK:-$HOME/work}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# 1. wait for the background download (tmux session "dl") and verify it
while tmux has-session -t dl 2>/dev/null; do
  echo "[run] download still running: $(du -sh "$WORK/input" | cut -f1) so far; checking again in 60s"; sleep 60
done
( cd "$WORK" && bash get-input.sh | tail -2 )

# 2. python environment
if [ ! -x "$WORK/.venv/bin/python" ]; then
  sudo apt-get install -y -qq python3-venv >/dev/null
  python3 -m venv "$WORK/.venv"
fi
"$WORK/.venv/bin/pip" install -q -r "$HERE/requirements.txt"

# 3. run
OUT="$WORK/attempt-$N"
if [ -e "$OUT" ]; then echo "[run] $OUT exists; remove it or use a new attempt number"; exit 1; fi
cd "$HERE"
nice "$WORK/.venv/bin/python" -m deid.pipeline --input "$WORK/input" --out "$OUT" \
  --private "$WORK/private" --workers "$(nproc)" 2>&1 | tee "$WORK/private/attempt-$N.log"
echo "[run] corpus files: $(find "$OUT/corpus" -type f | wc -l)  size: $(du -sh "$OUT/corpus" | cut -f1)"
echo "[run] DONE attempt-$N"
