#!/usr/bin/env bash
# Pull a git bundle of the production vault onto this Mac.
#
#   pull-vault-bundle.sh           write $DEST/vault-YYYY-MM-DD.bundle and keep
#                                  the newest 30
#   pull-vault-bundle.sh --help
#
# Pull-based on purpose: the server holds no credential for this copy, so a
# compromised server cannot delete it. art.exscalia.vault-pull.plist runs it
# daily. Settings come from ~/.config/exscalia/backup.env, never from the repo
# (the repo is mirrored publicly):
#   SERVER=root@your-server
#   DEST="$HOME/Backups/exscalia"
# Restore: git clone vault-YYYY-MM-DD.bundle vault
set -euo pipefail

CONFIG="$HOME/.config/exscalia/backup.env"
KEEP=30

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

if [[ ! -f "$CONFIG" ]]; then
  echo "pull-vault-bundle: $CONFIG is missing" >&2
  usage >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$CONFIG"
: "${SERVER:?is not set in $CONFIG}"
: "${DEST:?is not set in $CONFIG}"

mkdir -p "$DEST"
out="$DEST/vault-$(date +%F).bundle"
partial="$out.partial"
scratch="$(mktemp -d)"
trap 'rm -rf "$partial" "$scratch"' EXIT

# BatchMode: under launchd a password or host-key prompt would hang forever.
ssh -o BatchMode=yes -o ConnectTimeout=30 "$SERVER" \
  'docker compose -f omnianote/docker-compose.yml exec -T app git -C /data/vault bundle create - --all' \
  > "$partial"

# `bundle verify` needs a repository and checks only the header, so a
# truncated pack still passes it. Unbundling into a scratch repo runs
# index-pack over every object.
git init -q --bare "$scratch"
git -C "$scratch" bundle verify -q "$partial"
git -C "$scratch" bundle unbundle "$partial" > /dev/null

mv "$partial" "$out"
echo "pull-vault-bundle: wrote $out"

# The names sort by date, so everything before the newest $KEEP goes.
shopt -s nullglob
bundles=("$DEST"/vault-*.bundle)
if (( ${#bundles[@]} > KEEP )); then
  rm -f -- "${bundles[@]:0:${#bundles[@]}-KEEP}"
fi
