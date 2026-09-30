#!/usr/bin/env bash
# End-to-end smoke test of a running WiSAR container: real pipeline, real data.
#
#   WISAR_URL=http://localhost:8000 WISAR_TOKEN=<CloudTAK token> tests/api_smoke.sh [tarr|travel-time]
#
# WISAR_TOKEN may be empty when the container runs with WISAR_AUTH=none.
# Outputs are saved under ./smoke-out/<job-id>/. Needs curl and python3.
set -euo pipefail

BASE="${WISAR_URL:-http://localhost:8000}/api/v1"
MODE="${1:-travel-time}"
AUTH=()
[ -n "${WISAR_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer ${WISAR_TOKEN}")
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

echo "== health"
curl -fsS "$BASE/health"; echo

echo "== profiles"
curl -fsS ${AUTH[@]+"${AUTH[@]}"} "$BASE/profiles" | json "len(d['datasets'][0]['categories']), 'categories in', d['default_dataset']"

# West Fork of Oak Creek trailhead (the README example area).
IPP='{"lat": 34.9867, "lon": -111.7474}'
if [ "$MODE" = tarr ]; then
  BODY="{\"ipp\": $IPP, \"subject\": {\"kind\": \"listed\", \"category\": \"Hiker\", \"eco_region\": \"Dry\", \"terrain\": \"Mountainous\"}}"
  URL="$BASE/tarr/jobs"
else
  BODY="{\"ipp\": $IPP, \"speed\": {\"value\": 1.5, \"unit\": \"mph\"}, \"intervals_hours\": [1, 2]}"
  URL="$BASE/travel-time/jobs"
fi

echo "== submit $MODE"
RESP=$(curl -fsS ${AUTH[@]+"${AUTH[@]}"} -H 'Content-Type: application/json' -d "$BODY" "$URL")
ID=$(echo "$RESP" | json "d['id']")
echo "job $ID"; echo "$RESP" | json "json.dumps(d['resolved'])"

echo "== poll"
START=$(date +%s)
while :; do
  JOB=$(curl -fsS ${AUTH[@]+"${AUTH[@]}"} "$BASE/jobs/$ID")
  STATUS=$(echo "$JOB" | json "d['status']")
  printf '\r  %-10s %4ss' "$STATUS" "$(( $(date +%s) - START ))"
  case "$STATUS" in succeeded|failed) echo; break;; esac
  sleep 5
done
if [ "$STATUS" = failed ]; then echo "$JOB" | json "d['error']"; exit 1; fi
echo "$JOB" | json "'cell %s m, %d contours, warnings: %s' % (d['result']['cell_size_m'], d['result']['contour_count'], [w['message'][:60] for w in d['result']['warnings']])"

echo "== download"
OUT="smoke-out/$ID"; mkdir -p "$OUT"
for NAME in $(echo "$JOB" | json "' '.join(d['outputs'])"); do
  curl -fsS ${AUTH[@]+"${AUTH[@]}"} -o "$OUT/$NAME" "${WISAR_URL:-http://localhost:8000}/api/v1/jobs/$ID/outputs/$NAME"
  printf '  %-22s %8s bytes\n' "$NAME" "$(wc -c < "$OUT/$NAME" | tr -d ' ')"
done
echo "done: $OUT"
