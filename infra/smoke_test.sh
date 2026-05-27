#!/usr/bin/env bash
# Curl-based smoke test for a running QueryMind backend.
#
# Verifies the live API path: health -> register -> login -> create
# workspace folder -> add connection -> wait for profiling -> chat
# (SSE). Does NOT need the frontend. Requires the backend on $API and
# (for a non-trivial chat answer) a reachable LLM + a real data DB to
# point the connection at.
#
# Updated for Phase 1 multi-connection workspaces: a workspace is now
# a folder (POST /workspaces takes just `{name}`); each database lives
# in a separate WorkspaceConnection (POST /workspaces/{id}/connections).
#
# Usage:
#   API=http://localhost:8080 \
#   DATA_HOST=localhost DATA_PORT=5432 DATA_DB=sales_demo \
#   DATA_USER=querymind DATA_PASS=querymind \
#   ./infra/smoke_test.sh
set -euo pipefail

API="${API:-http://localhost:8080}"
EMAIL="${EMAIL:-smoke+$(date +%s)@test.local}"
PASSWORD="${PASSWORD:-supersecret123}"
DIALECT="${DIALECT:-postgres}"
DATA_HOST="${DATA_HOST:-localhost}"
DATA_PORT="${DATA_PORT:-5432}"
DATA_DB="${DATA_DB:-sales_demo}"
DATA_USER="${DATA_USER:-querymind}"
DATA_PASS="${DATA_PASS:-querymind}"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$1"; }
fail() { printf '\033[1;31mFAIL: %s\033[0m\n' "$1" >&2; exit 1; }

say "1. Health"
curl -fsS "$API/healthz" | grep -q '"status":"ok"' || fail "healthz not ok"
echo "ok"

say "2. Register"
curl -fsS -X POST "$API/auth/register" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" >/dev/null \
  || fail "register failed (email may already exist)"
echo "registered $EMAIL"

say "3. Login"
TOKEN=$(curl -fsS -X POST "$API/auth/login" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode "username=$EMAIL" \
  --data-urlencode "password=$PASSWORD" \
  | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
[ -n "$TOKEN" ] || fail "no access_token returned"
echo "got token"

say "4. Create workspace (folder)"
WS=$(curl -fsS -X POST "$API/workspaces" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"name\": \"smoke_$(date +%s)\"}")
WS_ID=$(echo "$WS" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
[ -n "$WS_ID" ] || fail "no workspace id: $WS"
echo "workspace $WS_ID"

say "5. Add connection ($DIALECT)"
# Build the connection_meta inline depending on the dialect. Smoke
# defaults to Postgres; override via DIALECT=mysql/clickhouse/oracle/
# mongodb/elasticsearch in the env (and adjust DATA_PORT accordingly).
case "$DIALECT" in
  postgres|mysql|clickhouse|oracle)
    META="{\"host\":\"$DATA_HOST\",\"port\":$DATA_PORT,\"db_name\":\"$DATA_DB\"}"
    CREDS="{\"user\":\"$DATA_USER\",\"password\":\"$DATA_PASS\"}"
    AUTH="password"
    ;;
  mongodb)
    META="{\"host\":\"$DATA_HOST\",\"port\":$DATA_PORT,\"db_name\":\"$DATA_DB\"}"
    CREDS="{\"user\":\"$DATA_USER\",\"password\":\"$DATA_PASS\"}"
    AUTH="password"
    ;;
  elasticsearch)
    META="{\"hosts\":[\"http://$DATA_HOST:$DATA_PORT\"],\"verify_certs\":false}"
    CREDS="{\"user\":\"$DATA_USER\",\"password\":\"$DATA_PASS\"}"
    AUTH="password"
    ;;
  sqlite)
    META="{\"path\":\"$DATA_DB\"}"
    CREDS="{}"
    AUTH="none"
    ;;
  *)
    fail "unknown DIALECT: $DIALECT"
    ;;
esac

CONN=$(curl -fsS -X POST "$API/workspaces/$WS_ID/connections" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{
        \"name\": \"primary\",
        \"dialect\": \"$DIALECT\",
        \"connection_meta\": $META,
        \"auth_kind\": \"$AUTH\",
        \"credentials\": $CREDS
      }")
CONN_ID=$(echo "$CONN" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
[ -n "$CONN_ID" ] || fail "no connection id: $CONN"
echo "connection $CONN_ID"

say "6. Wait for connection to reach 'ready' (needs Celery worker)"
for i in $(seq 1 20); do
  STATUS=$(curl -fsS "$API/workspaces/$WS_ID/connections/$CONN_ID" \
    -H "Authorization: Bearer $TOKEN" \
    | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
  echo "  status=$STATUS"
  [ "$STATUS" = "ready" ] && break
  [ "$STATUS" = "error" ] && fail "profiling errored"
  [ "$STATUS" = "auth_error" ] && fail "auth_error — bad credentials"
  sleep 2
done
[ "${STATUS:-}" = "ready" ] || echo "  (still $STATUS — is the Celery worker up?)"

say "7. Chat (SSE)"
curl -fsS -N -X POST "$API/chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{
        \"message\":\"total revenue by region\",
        \"active_workspace_id\":\"$WS_ID\",
        \"active_connection_id\":\"$CONN_ID\"
      }" \
  | sed 's/^/  /' | head -60

printf '\n\033[1;32mSmoke test reached the chat stream.\033[0m\n'
