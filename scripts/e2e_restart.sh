#!/usr/bin/env bash
# End-to-end proof that an approval pause survives a real service restart, against docker compose.
#
#   ./scripts/e2e_restart.sh
#
# Flow: employee asks for a change past the cut-off -> agrees to ask the manager -> `docker compose restart api`
# -> manager approves -> the employee's thread contains the approval and the roster moved.
# Uses the deterministic rule-based interpreter, so no LLM key is needed.
set -euo pipefail

cd "$(dirname "$0")/.."
BASE=${BASE:-http://localhost:8000}
export LLM_PROVIDER=rule_based
export ENV=dev

say() { printf '\n== %s\n' "$*"; }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
post() { curl -sf -X POST "$BASE$1" -H 'content-type: application/json' ${3:+-H "authorization: Bearer $3"} -d "$2"; }
wait_ready() { for _ in $(seq 1 60); do curl -sf "$BASE/ready" >/dev/null && return 0; sleep 1; done; echo "api not ready"; exit 1; }

say "fresh stack (wipes the demo volume)"
docker compose down -v >/dev/null 2>&1 || true
TODAY=$(TZ=Asia/Kolkata date +%F)
export CLOCK_OVERRIDE="${TODAY}T17:30:00+05:30"   # after Acme's 5:00 PM cut-off for the 7:00 PM drop
docker compose up -d --build >/dev/null
wait_ready

EMP=$(post /dev/token '{"company_id":"acme","employee_id":"A001"}' | json "d['token']")
MGR=$(post /dev/token '{"company_id":"acme","employee_id":"AM01"}' | json "d['token']")

say "1. employee asks for 8:30 PM (past the cut-off)"
R1=$(post /chat '{"message":"change my drop today to 8:30 pm"}' "$EMP")
echo "$R1" | json "d['reply']"
SESSION=$(echo "$R1" | json "d['session_id']")

say "2. employee agrees to ask the manager"
R2=$(post /chat "{\"session_id\":\"$SESSION\",\"message\":\"yes, ask my manager\"}" "$EMP")
echo "$R2" | json "d['reply']"
REQ=$(echo "$R2" | json "d['request']['id']")
echo "$R2" | json "'status=' + d['status']"
[ "$(echo "$R2" | json "d['status']")" = "pending_approval" ] || { echo "FAIL: not pending"; exit 1; }

say "3. RESTART the api container (all in-memory state is gone)"
docker compose restart api >/dev/null
wait_ready

say "4. manager approves $REQ"
post "/manager/requests/$REQ/decision" '{"decision":"approve"}' "$MGR" | json "d['reply']"

say "5. employee thread now contains the decision"
HISTORY=$(curl -sf "$BASE/chat/$SESSION/messages" -H "authorization: Bearer $EMP")
echo "$HISTORY" | json "d['messages'][-1]['content']"
echo "$HISTORY" | json "d['messages'][-1]['content']" | grep -q "approved" || { echo "FAIL: no approval message"; exit 1; }

say "6. roster moved"
curl -sf "$BASE/me/roster" -H "authorization: Bearer $EMP" | json "[e['scheduled_at'] for e in d['entries'] if e['id']=='RE-A001-D0-DROP'][0]"
curl -sf "$BASE/me/roster" -H "authorization: Bearer $EMP" | json "[e['trip_id'] for e in d['entries'] if e['id']=='RE-A001-D0-DROP'][0]" | grep -q "2030" || { echo "FAIL: roster not moved"; exit 1; }

echo; echo "PASS: approval survived a restart"
docker compose down >/dev/null 2>&1
