#!/usr/bin/env bash
# Container entry point: start HydraDB, load the code graph, serve the demo.
set -euo pipefail

ROOT=/tmp/hydradb
mkdir -p "$ROOT/store" "$ROOT/cache"
printf '%s\n' "${HYDRADB_TOKEN:-local-development-token-32-bytes}" > "$ROOT/auth-token"
export HYDRADB_TOKEN="${HYDRADB_TOKEN:-local-development-token-32-bytes}"

CLOUD_PROVIDER=local LOCAL_PATH="$ROOT/store" \
GRAPH_NAMESPACE=default GRAPH_ID=default GRAPH_CELL_ID=cell-0 GRAPH_CELLS=cell-0 GRAPH_NODE_ID=node-0 \
GRAPH_BOLT_NODE_ADDRESSES=node-0=127.0.0.1:7687 GRAPH_ADVERTISED_BOLT_ADDR=127.0.0.1:7687 \
GRAPH_DATA_CACHE_DIR="$ROOT/cache" GRAPH_DATA_CACHE_PART_BYTES=4194304 GRAPH_DATA_CACHE_MAX_OPEN_FILES=512 \
GRAPH_AUTH_TOKEN_FILE="$ROOT/auth-token" GRAPH_ALLOW_PLAINTEXT=true RUST_MIN_STACK=33554432 RUST_LOG=warn \
  /opt/hydradb/graph-node > "$ROOT/hydradb.log" 2>&1 &

for _ in $(seq 1 60); do
  curl -sf http://127.0.0.1:8443/healthz > /dev/null && break
  sleep 1
done
echo "HydraDB is up"

trailmark doctor
trailmark ingest /data/reef-base --name reef --slot 2

exec python -m trailmark.web --name reef --commits /data/reef --since "$(cat /data/reef-base.rev)" \
  --summary /app/summary.json --host 0.0.0.0 --port 7860
