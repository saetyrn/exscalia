#!/usr/bin/env bash
# Nightly off-site backup of the EXSCALIA data volume and the server .env.
#
#   omnianote-backup.sh                back up, then apply the retention policy
#   omnianote-backup.sh restic ARGS    any restic command against the same
#                                      repository (init, snapshots, check)
#   omnianote-backup.sh --help
#
# Runs on the VPS host from omnianote-backup.timer and needs nothing from the
# app beyond one sqlite snapshot: restic runs from its official image, so
# nothing is installed on the host. Configuration is /etc/omnianote-backup.env
# (see omnianote-backup.env.example); create the repository once with
# `omnianote-backup.sh restic init`.
#
# The app's own backup pushes only /data/vault. This copies the whole volume
# (minus the rebuildable index and model caches) plus ~/omnianote/.env, so API
# keys, the VAPID keypair, sessions and device tokens survive losing the VPS.
set -euo pipefail

ENV_FILE=/etc/omnianote-backup.env
APP_DIR=/root/omnianote
# Compose names volumes <project>_<volume>, and the project is the directory
# name (deploy.sh relies on the same name).
VOLUME=omnianote_omnianote-data
CACHE_DIR=/var/cache/omnianote-backup
# Every snapshot is filed under one host name. Inside a throwaway container
# restic would otherwise record a new random hostname each night, and
# `forget` (which groups by host) would never expire anything.
SNAPSHOT_HOST=omnianote

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  ""|restic) ;;
  *) usage >&2; exit 2 ;;
esac

if [[ ! -f "$ENV_FILE" ]]; then
  echo "omnianote-backup: $ENV_FILE is missing" >&2
  usage >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  # Checked because a bind mount of a missing file makes docker create a
  # directory in its place, which would then break `docker compose`.
  echo "omnianote-backup: $APP_DIR/.env is missing" >&2
  exit 1
fi

# A password file named in the env file lives on the host; hand it to the
# container at the same path.
password_mount=()
password_file="$(sed -n 's/^RESTIC_PASSWORD_FILE=//p' "$ENV_FILE" | tail -n 1)"
if [[ -n "$password_file" ]]; then
  password_mount=(-v "$password_file:$password_file:ro")
fi

mkdir -p "$CACHE_DIR"

restic() {
  docker run --rm \
    -v "$VOLUME:/data:ro" \
    -v "$APP_DIR/.env:/backup-env/.env:ro" \
    -v "$CACHE_DIR:/root/.cache/restic" \
    ${password_mount[@]+"${password_mount[@]}"} \
    --env-file "$ENV_FILE" \
    restic/restic "$@"
}

if [[ "${1:-}" == restic ]]; then
  shift
  restic "$@"
  exit
fi

# auth.sqlite3 is written live (sessions, device tokens, push subscriptions),
# so copying the file could catch it mid-write. SQLite's backup API takes a
# consistent copy into the volume; restic picks that up and skips the live file.
snapshot_auth_db='
import os, sqlite3
os.makedirs("/data/backup-staging", mode=0o700, exist_ok=True)
dst = "/data/backup-staging/auth.sqlite3"
if os.path.exists(dst):
    os.remove(dst)
s = sqlite3.connect("/data/auth.sqlite3")
d = sqlite3.connect(dst)
s.backup(d)
d.close()
s.close()
os.chmod(dst, 0o600)
'
compose() { docker compose -f "$APP_DIR/docker-compose.yml" "$@"; }
if [[ -n "$(compose ps --status running --quiet app)" ]]; then
  compose exec -T app python -c "$snapshot_auth_db"
else
  # A crash-looping app is when this backup matters most: take the snapshot
  # from a one-off container of the same image instead.
  echo "omnianote-backup: app is not running; snapshotting from a one-off container" >&2
  compose run --rm --no-deps -T app python -c "$snapshot_auth_db"
fi

restic backup --host "$SNAPSHOT_HOST" /data /backup-env \
  --exclude /data/index \
  --exclude /data/fastembed-cache \
  --exclude /data/hf-cache \
  --exclude /data/auth.sqlite3 \
  --exclude '/data/auth.sqlite3-*'

restic forget --host "$SNAPSHOT_HOST" \
  --keep-daily 7 --keep-weekly 8 --keep-monthly 6 --prune
