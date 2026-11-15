#!/usr/bin/env bash
# Starts a local single-node HydraDB (foreground). Data lives in ~/.hydradb.
set -euo pipefail
ROOT="$HOME/.hydradb"
# The local filesystem store cannot take writes after a restart (the object
# store's PutMode::Update is not implemented for it), so every start begins
# from an empty store. Reload with `trailmark ingest`.
rm -rf "$ROOT/store" "$ROOT/cache"
mkdir -p "$ROOT/store" "$ROOT/cache"
[ -f "$ROOT/auth-token" ] || printf '%s\n' 'local-development-token-32-bytes' > "$ROOT/auth-token"
export CLOUD_PROVIDER=local
export LOCAL_PATH="$ROOT/store"
export GRAPH_NAMESPACE=default
export GRAPH_ID=default
export GRAPH_CELL_ID=cell-0
export GRAPH_CELLS=cell-0
export GRAPH_NODE_ID=node-0
export GRAPH_BOLT_NODE_ADDRESSES=node-0=127.0.0.1:7687
export GRAPH_ADVERTISED_BOLT_ADDR=127.0.0.1:7687
export GRAPH_DATA_CACHE_DIR="$ROOT/cache"
export GRAPH_DATA_CACHE_PART_BYTES=4194304
export GRAPH_DATA_CACHE_MAX_OPEN_FILES=512
export GRAPH_AUTH_TOKEN_FILE="$ROOT/auth-token"
export GRAPH_ALLOW_PLAINTEXT=true
export RUST_MIN_STACK=33554432
exec "$HOME/hydradb/target/release/graph-node"
