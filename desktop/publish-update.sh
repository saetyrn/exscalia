#!/usr/bin/env bash
# Publish a desktop release to the update feed at notes.exscalia.art/updates.
#
#   ./publish-update.sh user@server [--notes "what changed"] [--dry-run]
#
# Publishes whatever signed artifacts exist:
#   - macOS:   src-tauri/target/release/bundle/macos/EXSCALIA.app.tar.gz (+ .sig)
#              (produced by `npx tauri build` with createUpdaterArtifacts)
#   - Windows: win-artifacts/EXSCALIA_<version>_x64-setup.exe
#              (downloaded unsigned from the GitHub Actions run; optional —
#              signed here, which writes the .sig beside it)
#
# The macOS tarball's name carries no version, so the Info.plist inside it
# must match tauri.conf.json, and the stamp build-frontend.sh left at
# src-tauri/target/build-sha must be clean and name the commit of tag
# v<version>. --dry-run stages and validates without touching the server;
# BUNDLE_DIR and BUILD_SHA_FILE point it at a fabricated bundle for testing.
#
# The private signing key stays on this machine (~/.tauri/exscalia.key) —
# never in the repo and never in CI.
set -euo pipefail

usage='usage: publish-update.sh user@server [--notes "..."] [--dry-run]'
die() { echo "error: $*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
SERVER="${1:-}"
[[ -n "$SERVER" && "$SERVER" != -* ]] || die "$usage"
shift
NOTES=""
DRY_RUN=0
while (($#)); do
  case "$1" in
    --notes) (($# >= 2)) || die "$usage"; NOTES="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) die "$usage" ;;
  esac
done

VERSION=$(python3 -c "import json; print(json.load(open('$HERE/src-tauri/tauri.conf.json'))['version'])")
BUNDLE="${BUNDLE_DIR:-$HERE/src-tauri/target/release/bundle}"
STAMP_FILE="${BUILD_SHA_FILE:-$HERE/src-tauri/target/build-sha}"
FEED="https://notes.exscalia.art/updates"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

STAMP=""
if [[ -f "$STAMP_FILE" ]]; then STAMP="$(tr -d '[:space:]' < "$STAMP_FILE")"; fi

MAC_TARBALL="$BUNDLE/macos/EXSCALIA.app.tar.gz"
if [[ ! -f "$MAC_TARBALL" ]]; then
  MAC_TARBALL=""
elif [[ ! -f "$MAC_TARBALL.sig" ]]; then
  echo "note: $MAC_TARBALL has no .sig — skipping macOS" >&2
  MAC_TARBALL=""
fi

if [[ -n "$MAC_TARBALL" ]]; then
  # A skipped or failed mac build leaves the previous release's tarball (and
  # its valid .sig) here; shipped as $VERSION it would relaunch as the old
  # version and re-offer the update on every launch.
  tar_version=$(tar -xzOf "$MAC_TARBALL" EXSCALIA.app/Contents/Info.plist \
    | plutil -extract CFBundleShortVersionString raw -o - -) \
    || die "cannot read the version inside $MAC_TARBALL"
  if [[ "$tar_version" != "$VERSION" ]]; then
    die "$MAC_TARBALL holds $tar_version, not $VERSION — rebuild, or remove it to publish Windows only"
  fi
  APP_PLIST="$BUNDLE/macos/EXSCALIA.app/Contents/Info.plist"
  if [[ -f "$APP_PLIST" ]]; then
    app_version=$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP_PLIST")
    if [[ "$app_version" != "$VERSION" ]]; then
      die "$BUNDLE/macos/EXSCALIA.app is $app_version, not $VERSION — rebuild"
    fi
  fi

  # The build runs from the working tree, so the frontend inside must be
  # proven to be the tagged source. HEAD may have moved on since (the feed is
  # re-published after Windows CI), so compare the stamp with the tag.
  [[ -n "$STAMP" ]] || die "no build stamp at $STAMP_FILE — rebuild with \`npx tauri build\`"
  if [[ "$STAMP" == *-dirty ]]; then
    die "the macOS build ($STAMP) had uncommitted changes in frontend/ or desktop/ — commit and rebuild"
  fi
  [[ "$STAMP" =~ ^[0-9a-f]{7,40}$ ]] || die "build stamp '$STAMP' in $STAMP_FILE is not a commit"
  if [[ "$STAMP_FILE" -nt "$MAC_TARBALL" ]]; then
    die "$STAMP_FILE is newer than $MAC_TARBALL — the last build did not produce this tarball; rebuild"
  fi
  tag_commit=$(git -C "$HERE/.." rev-parse --verify --quiet "v$VERSION^{commit}") \
    || die "tag v$VERSION not found"
  stamp_commit=$(git -C "$HERE/.." rev-parse --verify --quiet "$STAMP^{commit}") || stamp_commit=""
  if [[ "$stamp_commit" != "$tag_commit" ]]; then
    die "the macOS build is from $STAMP, not v$VERSION ($(git -C "$HERE/.." rev-parse --short "$tag_commit")) — check out the tag and rebuild"
  fi
  echo "macOS $VERSION built from $STAMP"
else
  echo "note: not publishing macOS — build stamp ${STAMP:-(none)} not checked" >&2
fi

# Pinned to the version being published: a stale installer left in
# win-artifacts/ must never ship under a newer version number (a bare
# `*-setup.exe | head -1` nearly served 0.1.5 as 0.1.6 once).
WIN_EXE=$(ls "$HERE"/win-artifacts/*"_${VERSION}_"*-setup.exe 2>/dev/null | head -1 || true)
if [[ -z "$WIN_EXE" ]] && ls "$HERE"/win-artifacts/*-setup.exe >/dev/null 2>&1; then
  echo "note: win-artifacts/ holds installers for a different version — skipping Windows" >&2
fi
# CI builds the installer unsigned, so sign it here. Always re-sign: a
# re-downloaded installer must never ship with a .sig left over from an
# earlier build of the same version. An exported TAURI_SIGNING_PRIVATE_KEY
# would clash with -f, hence env -u.
if [[ -n "$WIN_EXE" ]]; then
  (cd "$HERE" && env -u TAURI_SIGNING_PRIVATE_KEY -u TAURI_SIGNING_PRIVATE_KEY_PATH \
    npx tauri signer sign -f "$HOME/.tauri/exscalia.key" -p "" "$WIN_EXE" >/dev/null)
  echo "signed $(basename "$WIN_EXE")"
fi

# The sha goes into the notes only when it describes a staged artifact.
NOTE_SHA=""
if [[ -n "$MAC_TARBALL" ]]; then NOTE_SHA="$STAMP"; fi

mkdir "$STAGE/artifacts"
python3 - "$VERSION" "$NOTES" "$NOTE_SHA" "$MAC_TARBALL" "$WIN_EXE" "$STAGE" "$FEED" <<'EOF'
import json, pathlib, shutil, sys
from datetime import datetime, timezone

version, notes, sha, mac_tarball, win_exe, stage, base = sys.argv[1:8]
stage = pathlib.Path(stage)
platforms = {}

def add(platform_key, artifact):
    artifact = pathlib.Path(artifact)
    sig = artifact.with_suffix(artifact.suffix + ".sig")
    if not (artifact.is_file() and sig.is_file()):
        return False
    name = f"{version}-{artifact.name}"
    shutil.copy2(artifact, stage / "artifacts" / name)
    platforms[platform_key] = {
        "signature": sig.read_text().strip(),
        "url": f"{base}/{name}",
    }
    return True

mac_ok = add("darwin-aarch64", mac_tarball) if mac_tarball else False
win_ok = add("windows-x86_64", win_exe) if win_exe else False
if not (mac_ok or win_ok):
    sys.exit("no signed artifacts found — run `npx tauri build` first")

notes = notes or f"EXSCALIA Desktop {version}"
if sha:
    notes = f"{notes} ({sha})"
(stage / "latest.json").write_text(json.dumps({
    "version": version,
    "notes": notes,
    "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "platforms": platforms,
}, indent=2))
print(f"staged {version}: {', '.join(platforms)}")
EOF

if ((DRY_RUN)); then
  echo "==> Dry run: nothing sent to $SERVER"
  ls -l "$STAGE/artifacts"
  cat "$STAGE/latest.json"
  echo
  exit 0
fi

# Artifacts first, then the feed under a temp name renamed into place on the
# server, so no client ever reads a latest.json whose files are not there yet.
echo "==> Publishing to $SERVER:omnianote/updates"
ssh "$SERVER" "mkdir -p omnianote/updates"
scp -q "$STAGE"/artifacts/* "$SERVER:omnianote/updates/"
scp -q "$STAGE/latest.json" "$SERVER:omnianote/updates/latest.json.tmp"
ssh "$SERVER" "mv -f omnianote/updates/latest.json.tmp omnianote/updates/latest.json"

# Keep the three newest versions of each platform's installer (named
# <version>-<artifact> above); older ones only fill the server's disk.
# latest.json and the version just published are never removed.
LISTING="$(ssh "$SERVER" 'ls -1 omnianote/updates')" || LISTING=""
STALE="$(python3 - "$VERSION" "$LISTING" <<'PRUNE'
import re, sys

current, listing = sys.argv[1], sys.argv[2].split()
platforms = {}
for name in listing:
    match = re.fullmatch(r"(\d+(?:\.\d+)*)-([A-Za-z0-9._-]+)", name)
    if match:
        version, artifact = match.groups()
        key = artifact.replace(version, "{version}")
        platforms.setdefault(key, []).append((tuple(map(int, version.split("."))), version, name))
for files in platforms.values():
    files.sort(reverse=True)
    for _, version, name in files[3:]:
        if version != current:
            print(name)
PRUNE
)"
if [[ -n "$STALE" ]]; then
  echo "==> Removing older installers:" $STALE
  ssh "$SERVER" "cd omnianote/updates && rm -f" $STALE || echo "note: could not remove them" >&2
fi

echo "==> Verifying $FEED/latest.json"
urls=$(curl -fsS "$FEED/latest.json" | python3 -c '
import json, sys
feed = json.load(sys.stdin)
served = feed.get("version")
if served != sys.argv[1]:
    sys.exit(f"feed serves {served}, expected {sys.argv[1]}")
for platform in feed["platforms"].values():
    print(platform["url"])
' "$VERSION") || die "$FEED/latest.json does not serve $VERSION"
failed=0
for url in $urls; do
  if curl -fsSI -o /dev/null "$url"; then
    echo "ok $url"
  else
    echo "FAILED $url" >&2
    failed=1
  fi
done
((failed == 0)) || die "the feed serves $VERSION but not every artifact is reachable"
echo "Done: $FEED/latest.json serves $VERSION"
