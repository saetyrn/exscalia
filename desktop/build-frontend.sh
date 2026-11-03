#!/usr/bin/env bash
# Tauri beforeBuildCommand on macOS (see tauri.conf.json; Windows CI keeps the
# plain npm build via tauri.windows.conf.json). Builds ../frontend with the
# commit it came from baked in as __BUILD_SHA__ and stamps the same value at
# src-tauri/target/build-sha, which publish-update.sh checks against the
# release tag. "-dirty" = frontend/ or desktop/ had uncommitted changes.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if SHA=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null); then
  if [[ -n "$(git -C "$HERE" status --porcelain -- :/frontend :/desktop)" ]]; then
    SHA="$SHA-dirty"
  fi
else
  SHA=unknown
fi

BUILD_SHA="$SHA" npm --prefix "$HERE/../frontend" run build

mkdir -p "$HERE/src-tauri/target"
printf '%s\n' "$SHA" > "$HERE/src-tauri/target/build-sha"
echo "frontend build $SHA"
