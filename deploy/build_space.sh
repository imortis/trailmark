#!/usr/bin/env bash
# Assemble the Hugging Face Space directory (deploy/build/space) from this repository.
#   HYDRADB_BIN   path to a release graph-node built from hydra-db/hydradb (default: WSL build)
#   HYDRADB_SRC   path to that HydraDB checkout, for its LICENSE
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
out="$here/deploy/build/space"
rm -rf "$out"
mkdir -p "$out/app" "$out/hydradb"

cp "$here/deploy/space/Dockerfile" "$here/deploy/space/README.md" "$out/"
cp "$here/deploy/space/start.sh" "$out/app/"
cp -r "$here/src" "$here/pyproject.toml" "$here/README.md" "$here/LICENSE" "$out/app/"
cp "$here/results/summary.json" "$out/app/summary.json"
find "$out/app" -name __pycache__ -type d -prune -exec rm -rf {} +

cp "${HYDRADB_BIN:?set HYDRADB_BIN to a release graph-node}" "$out/hydradb/graph-node"
cp "${HYDRADB_SRC:?set HYDRADB_SRC to the HydraDB checkout}/LICENSE" "$out/hydradb/LICENSE"
sed -i 's/\r$//' "$out/app/start.sh"
echo "assembled $out"
du -sh "$out"
