#!/usr/bin/env bash
# Publish a desktop release to the update feed at notes.exscalia.art/updates.
#
#   ./publish-update.sh [user@server] [--notes "what changed"]
#
# Publishes whatever signed artifacts exist:
#   - macOS:   src-tauri/target/release/bundle/macos/EXSCALIA.app.tar.gz (+ .sig)
#              (produced by `npx tauri build` with createUpdaterArtifacts)
#   - Windows: win-artifacts/*-setup.exe (+ .sig)
#              (downloaded from the GitHub Actions run; optional)
#
# The private signing key stays on this machine (~/.tauri/exscalia.key) and
# in the repo's GitHub secrets — never in the repo itself.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="${1:?usage: publish-update.sh user@server [--notes ...]}"
NOTES=""
if [[ "${2:-}" == "--notes" ]]; then NOTES="${3:-}"; fi

VERSION=$(python3 -c "import json; print(json.load(open('$HERE/src-tauri/tauri.conf.json'))['version'])")
BUNDLE="$HERE/src-tauri/target/release/bundle"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

MAC_TARBALL="$BUNDLE/macos/EXSCALIA.app.tar.gz"
# Pinned to the version being published: a stale installer left in
# win-artifacts/ must never ship under a newer version number (a bare
# `*-setup.exe | head -1` nearly served 0.1.5 as 0.1.6 once).
WIN_EXE=$(ls "$HERE"/win-artifacts/*"_${VERSION}_"*-setup.exe 2>/dev/null | head -1 || true)
if [[ -z "$WIN_EXE" ]] && ls "$HERE"/win-artifacts/*-setup.exe >/dev/null 2>&1; then
  echo "note: win-artifacts/ holds installers for a different version — skipping Windows" >&2
fi

python3 - "$VERSION" "$NOTES" "$MAC_TARBALL" "$WIN_EXE" "$STAGE" <<'EOF'
import json, pathlib, shutil, sys
from datetime import datetime, timezone

version, notes, mac_tarball, win_exe, stage = sys.argv[1:6]
stage = pathlib.Path(stage)
base = "https://notes.exscalia.art/updates"
platforms = {}

def add(platform_key, artifact):
    artifact = pathlib.Path(artifact)
    sig = artifact.with_suffix(artifact.suffix + ".sig")
    if not (artifact.is_file() and sig.is_file()):
        return False
    name = f"{version}-{artifact.name}"
    shutil.copy2(artifact, stage / name)
    platforms[platform_key] = {
        "signature": sig.read_text().strip(),
        "url": f"{base}/{name}",
    }
    return True

mac_ok = add("darwin-aarch64", mac_tarball) if mac_tarball else False
win_ok = add("windows-x86_64", win_exe) if win_exe else False
if not (mac_ok or win_ok):
    sys.exit("no signed artifacts found — run `npx tauri build` first")

(stage / "latest.json").write_text(json.dumps({
    "version": version,
    "notes": notes or f"EXSCALIA Desktop {version}",
    "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "platforms": platforms,
}, indent=2))
print(f"staged {version}: {', '.join(platforms)}")
EOF

echo "==> Publishing to $SERVER:omnianote/updates"
ssh "$SERVER" "mkdir -p omnianote/updates"
scp -q "$STAGE"/* "$SERVER:omnianote/updates/"
echo "Done: https://notes.exscalia.art/updates/latest.json now serves $VERSION"
