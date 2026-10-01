#!/usr/bin/env bash
# End-to-end smoke test of a running WiSAR container: real pipeline, real data.
#
#   WISAR_URL=http://localhost:8000 WISAR_TOKEN=<CloudTAK token> tests/api_smoke.sh [tarr|travel-time]
#
# WISAR_TOKEN may be empty when the container runs with WISAR_AUTH=none.
# Outputs are saved under ./smoke-out/<job-id>/. Needs curl and python3.
# Written for bash 3.2 (macOS) as well as bash 5.
set -euo pipefail

ROOT="${WISAR_URL:-http://localhost:8000}"
BASE="$ROOT/api/v1"
MODE="${1:-travel-time}"
TOKEN="${WISAR_TOKEN:-}"
AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer ${TOKEN}")
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

# api <curl args...> <url>
# Prints the response body on success. On a connection failure or an HTTP
# error it prints the status and the API's problem details to stderr and
# makes the caller fail (set -e), instead of a JSON traceback.
api() {
  local tmp code url
  url="${@: -1}"
  tmp=$(mktemp)
  if ! code=$(curl -sS -o "$tmp" -w '%{http_code}' ${AUTH[@]+"${AUTH[@]}"} "$@"); then
    rm -f "$tmp"
    echo "ERROR: could not connect to $url" >&2
    return 1
  fi
  if [ "$code" -ge 400 ]; then
    echo "ERROR: HTTP $code from $url" >&2
    python3 - "$tmp" >&2 <<'PY' || head -c 500 "$tmp" >&2
import json, sys
d = json.load(open(sys.argv[1]))
print('  ' + ': '.join(str(x) for x in (d.get('title'), d.get('detail')) if x))
for e in d.get('errors') or []:
    print('  %s %s' % (e.get('pointer'), e.get('detail')))
PY
    if [ "$code" = 401 ]; then
      echo "  hint: WISAR_TOKEN is ${#TOKEN} characters; it must be one CloudTAK token from the same CloudTAK this WiSAR checks against" >&2
    fi
    rm -f "$tmp"
    return 1
  fi
  cat "$tmp"
  rm -f "$tmp"
}

echo "== health"
api "$BASE/health"; echo

echo "== profiles"
PROFILES=$(api "$BASE/profiles")
echo "$PROFILES" | json "len(d['datasets'][0]['categories']), 'categories in', d['default_dataset']"

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
RESP=$(api -H 'Content-Type: application/json' -d "$BODY" "$URL")
ID=$(echo "$RESP" | json "d['id']")
echo "job $ID"; echo "$RESP" | json "json.dumps(d['resolved'])"

echo "== poll"
START=$(date +%s)
while :; do
  JOB=$(api "$BASE/jobs/$ID")
  STATUS=$(echo "$JOB" | json "d['status']")
  printf '\r  %-10s %4ss' "$STATUS" "$(( $(date +%s) - START ))"
  case "$STATUS" in succeeded|failed) echo; break;; esac
  sleep 5
done
if [ "$STATUS" = failed ]; then
  echo "$JOB" | json "'ERROR: job failed - %s: %s' % (d['error'].get('title'), d['error'].get('detail'))" >&2
  exit 1
fi
echo "$JOB" | json "'cell %s m, %d contours, warnings: %s' % (d['result']['cell_size_m'], d['result']['contour_count'], [w['message'][:60] for w in d['result']['warnings']])"

echo "== download"
OUT="smoke-out/$ID"; mkdir -p "$OUT"
for NAME in $(echo "$JOB" | json "' '.join(d['outputs'])"); do
  api "$BASE/jobs/$ID/outputs/$NAME" > "$OUT/$NAME"
  printf '  %-22s %8s bytes\n' "$NAME" "$(wc -c < "$OUT/$NAME" | tr -d ' ')"
done
echo "done: $OUT"
