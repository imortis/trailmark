#!/usr/bin/env bash
# Container entry point: start HydraDB, load the code graph, serve the demo.
# Every path and port can be overridden, so the same script runs outside the container.
set -euo pipefail

DATA="${TRAILMARK_DATA:-/data}"
ROOT="${HYDRADB_ROOT:-/tmp/hydradb}"
BIN="${HYDRADB_BIN:-/opt/hydradb/graph-node}"
HTTP_PORT="${HYDRADB_HTTP_PORT:-8443}"
BOLT_PORT="${HYDRADB_BOLT_PORT:-7687}"
ADMIN_PORT="${HYDRADB_ADMIN_PORT:-9090}"
PORT="${PORT:-7860}"
SUMMARY="${TRAILMARK_SUMMARY:-/app/summary.json}"

rm -rf "$ROOT/store" "$ROOT/cache"
mkdir -p "$ROOT/store" "$ROOT/cache"
export HYDRADB_TOKEN="${HYDRADB_TOKEN:-local-development-token-32-bytes}"
export HYDRADB_URL="http://127.0.0.1:$HTTP_PORT"
printf '%s\n' "$HYDRADB_TOKEN" > "$ROOT/auth-token"

CLOUD_PROVIDER=local LOCAL_PATH="$ROOT/store" \
GRAPH_NAMESPACE=default GRAPH_ID=default GRAPH_CELL_ID=cell-0 GRAPH_CELLS=cell-0 GRAPH_NODE_ID=node-0 \
GRAPH_HTTP_ADDR="127.0.0.1:$HTTP_PORT" GRAPH_BOLT_ADDR="127.0.0.1:$BOLT_PORT" GRAPH_ADMIN_ADDR="127.0.0.1:$ADMIN_PORT" \
GRAPH_BOLT_NODE_ADDRESSES="node-0=127.0.0.1:$BOLT_PORT" GRAPH_ADVERTISED_BOLT_ADDR="127.0.0.1:$BOLT_PORT" \
GRAPH_DATA_CACHE_DIR="$ROOT/cache" GRAPH_DATA_CACHE_PART_BYTES=4194304 GRAPH_DATA_CACHE_MAX_OPEN_FILES=512 \
GRAPH_AUTH_TOKEN_FILE="$ROOT/auth-token" GRAPH_ALLOW_PLAINTEXT=true RUST_MIN_STACK=33554432 RUST_LOG=warn \
  "$BIN" > "$ROOT/hydradb.log" 2>&1 &

for _ in $(seq 1 60); do
  curl -sf "$HYDRADB_URL/healthz" > /dev/null && break
  sleep 1
done
echo "HydraDB is up on $HYDRADB_URL"

trailmark doctor
trailmark ingest "$DATA/reef-base" --name reef --slot 2

exec python -m trailmark.web --name reef --commits "$DATA/reef" --since "$(cat "$DATA/reef-base.rev")" \
  --summary "$SUMMARY" --host 0.0.0.0 --port "$PORT"
