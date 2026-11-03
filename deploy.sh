#!/usr/bin/env bash
# One-command EXSCALIA deployment.
#
#   ./deploy.sh root@YOUR_SERVER_IP notes.yourdomain.com [--with-vault] [--skip-tests] [--allow-dirty] [--refresh]
#   ./deploy.sh root@YOUR_SERVER_IP notes.yourdomain.com --rollback
#
# Run from the repo root on your local machine. Requires ssh access to an
# Ubuntu/Debian server (your VPS provider gives you this when you add your
# SSH key at creation). --with-vault also copies your local data/vault to the
# server (first deploy only — it will not overwrite an existing server vault).
# That is a migration; restoring from a backup is RESTORE.md.
#
# The backend and frontend suites run locally first and a red suite stops the
# deploy; --skip-tests is the explicit escape hatch. What ships is the
# committed tree (HEAD), limited to what the server build needs: uncommitted
# edits are never deployed, and the script refuses to run with any in the
# shipped paths unless --allow-dirty says to ship HEAD without them.
#
# Each deploy builds omnianote-app:<commit> and keeps the image that was
# running as omnianote-app:prev. The new build must turn healthy and then
# answer https://DOMAIN with its own commit; if it does not, prev goes back
# into service under the compose file and Caddyfile of the last verified
# deploy (kept on the server as *.verified) and the script exits non-zero.
# --rollback does only that step.
#
# The base images and caddy are pinned by version, and the server reuses what
# it pulled. --refresh pulls those tags again first (OS and security rebuilds,
# patch releases); run it monthly.
#
# The script prompts for credentials (login username/password, optional AI keys, optional
# backup remote) directly in your terminal; they are written straight into
# the server's .env over ssh.
set -euo pipefail

SERVER="${1:-}"
DOMAIN="${2:-}"

usage() {
  echo "usage: ./deploy.sh user@server-ip your.domain.com [--with-vault] [--skip-tests] [--allow-dirty] [--refresh] [--rollback]" >&2
  echo "       --refresh pulls the pinned base images and caddy again; run it monthly." >&2
  exit 1
}

if [[ -z "$SERVER" || -z "$DOMAIN" || "$SERVER" == -* || "$DOMAIN" == -* ]]; then
  usage
fi
shift 2

WITH_VAULT=""
SKIP_TESTS=""
ALLOW_DIRTY=""
ROLLBACK=""
REFRESH=""
for arg in "$@"; do
  case "$arg" in
    --with-vault) WITH_VAULT=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --rollback) ROLLBACK=1 ;;
    --refresh) REFRESH=1 ;;
    *) echo "unknown option: $arg" >&2; usage ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="~/omnianote"

# Exactly what the server build needs (the Dockerfile COPYs only frontend/ and
# backend/), plus scripts/backup, which the host's systemd backup unit runs from
# ~/omnianote. git archive carries tracked files only, so .git, the vault,
# node_modules, build output and gitignored scratch can never ship.
SHIP=(Dockerfile docker-compose.yml Caddyfile .dockerignore
      backend/app backend/pyproject.toml backend/uv.lock frontend scripts/backup)

prompt_username() {
  while true; do
    read -r -p "    Choose a login username for the web UI [owner]: " APP_USERNAME
    APP_USERNAME="${APP_USERNAME:-owner}"
    if [[ "$APP_USERNAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
      return
    fi
    echo "    Use only letters, numbers, dots, underscores, or hyphens."
  done
}

# The image refuses to boot with an empty password (OMNIA_REQUIRE_AUTH).
prompt_password() {
  while true; do
    read -r -s -p "    Choose a login password for the web UI: " APP_PASSWORD; echo
    if [[ -n "$APP_PASSWORD" ]]; then
      return
    fi
    echo "    The password cannot be empty."
  done
}

# Put omnianote-app:prev, the image that ran before the last deploy, back in
# service.
rollback() {
  echo "==> Rolling back to the previous image"
  ssh "$SERVER" 'bash -s' <<'REMOTE'
set -euo pipefail
cd omnianote
if ! docker image inspect omnianote-app:prev >/dev/null 2>&1; then
  echo "    no omnianote-app:prev on the server: nothing to roll back to" >&2
  exit 1
fi
# A failed deploy leaves its own compose file and Caddyfile here, and `up`
# with them can fail the same way again (an option this engine rejects, an
# image that is not there). Go back to the last verified pair.
caddy_changed=""
if [ -f docker-compose.yml.verified ] && [ -f Caddyfile.verified ]; then
  cmp -s Caddyfile.verified Caddyfile || caddy_changed=1
  cp docker-compose.yml.verified docker-compose.yml
  cp Caddyfile.verified Caddyfile
fi
docker tag omnianote-app:prev omnianote-app:latest
compose=(docker compose -f docker-compose.yml)
if [ -z "$(docker image inspect -f '{{.Config.User}}' omnianote-app:prev)" ]; then
  # prev predates the non-root image and runs as root. Over a volume that now
  # belongs to uid 10001, root needs DAC_OVERRIDE and FOWNER to write and
  # chmod, and that image's CMD may write to the root fs. What it creates
  # stays root-owned until the next deploy hands it back to 10001.
  override="$(mktemp)"
  trap 'rm -f "$override"' EXIT
  cat > "$override" <<'YAML'
services:
  app:
    read_only: false
    cap_add:
      - DAC_OVERRIDE
      - FOWNER
YAML
  compose+=(-f "$override")
fi
if ! APP_TAG=latest "${compose[@]}" up -d --no-build --wait --wait-timeout 240; then
  # Last resort, whatever the compose file says: start what the deploy
  # stopped (the app, for the chown) as it was.
  docker ps -aq --filter label=com.docker.compose.project=omnianote --filter status=exited \
    | xargs -r docker start >/dev/null || true
  exit 1
fi
if [ -n "$caddy_changed" ]; then
  # cp wrote into the bind-mounted file; caddy reads it only when recreated.
  APP_TAG=latest "${compose[@]}" up -d --no-deps --force-recreate caddy
fi
REMOTE
}

fail() {
  echo "==> DEPLOY FAILED: $1" >&2
  if ! rollback; then
    echo "==> The rollback failed too. On the server: cd omnianote && docker compose ps && docker compose logs --tail 50 app" >&2
  fi
  exit 1
}

# The container healthcheck only proves the process answers /api/health from
# inside. From here, through DNS, TLS and Caddy: the new build must be the one
# answering, a route that reads app state must work, and the SPA must load.
# Retried for a while, since a first deploy may still be getting its
# certificate.
verify_live() {
  local attempt health="" status=""
  for attempt in $(seq 1 30); do
    health="$(curl -fsS --max-time 10 "https://$DOMAIN/api/health" 2>/dev/null)" || true
    if [[ "$health" == *"\"build\":\"$REV\""* ]]; then
      break
    fi
    sleep 4
  done
  if [[ "$health" != *"\"build\":\"$REV\""* ]]; then
    echo "    /api/health answered: ${health:-nothing}" >&2
    return 1
  fi
  status="$(curl -fsS --max-time 10 "https://$DOMAIN/api/auth/status")" || return 1
  if [[ "$status" != *'"auth_required":'* ]]; then
    echo "    /api/auth/status answered: $status" >&2
    return 1
  fi
  if [[ "$status" == *'"auth_required":false'* ]]; then
    echo "==> WARNING: https://$DOMAIN runs with authentication OFF (OMNIA_REQUIRE_AUTH=0)" >&2
  fi
  curl -fsS --max-time 10 -o /dev/null "https://$DOMAIN/"
}

if [[ -n "$ROLLBACK" ]]; then
  echo "==> Checking ssh access to $SERVER"
  ssh -o ConnectTimeout=10 "$SERVER" true
  rollback
  echo "==> https://$DOMAIN/api/health: $(curl -fsS --max-time 10 "https://$DOMAIN/api/health" || echo unreachable)"
  exit 0
fi

REV="$(git -C "$HERE" rev-parse --short HEAD)"
# Both passed to every compose call below: the image tag, and the commit the
# image reports at /api/health.
TAGGED="APP_TAG=$REV GIT_SHA=$REV"
DIRTY="$(git -C "$HERE" status --porcelain -- "${SHIP[@]}")"
if [[ -n "$DIRTY" ]]; then
  if [[ -z "$ALLOW_DIRTY" ]]; then
    echo "==> Refusing to deploy: these uncommitted changes would NOT ship; only HEAD ($REV) does." >&2
    printf '%s\n' "$DIRTY" | sed 's/^/      /' >&2
    echo "    Commit them, or rerun with --allow-dirty to ship HEAD without them." >&2
    exit 1
  fi
  echo "==> WARNING: these uncommitted changes will NOT be deployed; only HEAD ($REV) ships." >&2
  echo "    The test preflight still runs against the working tree, not HEAD." >&2
  printf '%s\n' "$DIRTY" | sed 's/^/      /' >&2
fi

echo "==> Checking ssh access to $SERVER"
ssh -o ConnectTimeout=10 "$SERVER" true

if [[ -n "$SKIP_TESTS" ]]; then
  echo "==> Skipping the test preflight (--skip-tests)"
else
  # Nothing else gates production: a red suite stops here, before anything
  # leaves this machine.
  echo "==> Preflight: backend tests"
  if [[ ! -f "$HERE/frontend/build/index.html" ]]; then
    # The backend's full-lifespan smoke test serves the built SPA.
    (cd "$HERE/frontend" && npm run build)
  fi
  (cd "$HERE/backend" && ~/.local/bin/uv run --locked --extra dev pytest -q -x -o faulthandler_timeout=120)
  echo "==> Preflight: frontend tests + type check"
  (cd "$HERE/frontend" && npm test && npm run check)
fi

echo "==> Shipping $REV to $SERVER:$REMOTE_DIR"
BUNDLE="$(mktemp)"
trap 'rm -f "$BUNDLE"' EXIT
git -C "$HERE" archive --format=tar.gz -o "$BUNDLE" HEAD -- "${SHIP[@]}"
# docker-compose.yml bind-mounts the Caddyfile, and a file bind mount keeps the
# inode it started with: extracting a new one leaves a running caddy on the old
# config. Compare before/after and recreate caddy below when it changed.
CADDY_BEFORE="$(ssh "$SERVER" 'sha256sum omnianote/Caddyfile 2>/dev/null | cut -d" " -f1')"
# rollback's *.verified pair: refreshed after every verified deploy below;
# this seeds it, from the files the running app came up with, on a server
# that has none yet.
ssh "$SERVER" 'bash -s' <<'REMOTE'
set -euo pipefail
[ -d omnianote ] || exit 0
cd omnianote
if [ ! -f docker-compose.yml.verified ] && [ -f docker-compose.yml ] && [ -f Caddyfile ]; then
  cp Caddyfile Caddyfile.verified
  cp docker-compose.yml docker-compose.yml.verified
fi
REMOTE
# Replace the source trees wholesale so files deleted from the repo go too.
ssh "$SERVER" 'mkdir -p omnianote && rm -rf omnianote/backend omnianote/frontend omnianote/scripts && tar -xz -C omnianote' < "$BUNDLE"
CADDY_AFTER="$(ssh "$SERVER" 'sha256sum omnianote/Caddyfile | cut -d" " -f1')"
# Earlier deploys rsynced the whole working tree, and rsync --delete never
# removed the paths it excluded. Drop those copies (idempotent); never touch
# .env, updates/ or vault-import/.
ssh "$SERVER" 'cd omnianote && rm -rf .git .github .claude .pstack .DS_Store .Rhistory output tmp HANDOFF.md AGENTS.md CLAUDE.md AUDIT-*.md desktop ipad'

echo "==> Installing Docker + firewall (idempotent)"
ssh "$SERVER" 'bash -s' <<'REMOTE'
set -euo pipefail
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22 >/dev/null; ufw allow 80 >/dev/null; ufw allow 443 >/dev/null
  ufw --force enable >/dev/null || true
fi
# Docker restarts a container only when it exits: an app that hangs but stays
# up would sit "unhealthy" forever. Restart it instead.
if [ -d /etc/cron.d ]; then
  cat > /etc/cron.d/omnianote-unhealthy <<'CRON'
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/5 * * * * root docker ps -q --filter health=unhealthy --filter label=com.docker.compose.project=omnianote | xargs -r docker restart >/dev/null
CRON
fi
REMOTE

# --- .env: create once, interactively -------------------------------------
if ssh "$SERVER" test -f omnianote/.env; then
  echo "==> Server .env already exists — leaving it untouched."
  # Older deployments had password-only authentication. Add a username once
  # so the next restart cannot retain password-only login.
  if ! ssh "$SERVER" "grep -q '^OMNIA_AUTH_USERNAME=' omnianote/.env"; then
    prompt_username
    printf 'OMNIA_AUTH_USERNAME=%s\n' "$APP_USERNAME" \
      | ssh "$SERVER" 'cat >> omnianote/.env'
  fi
  # One new setting since older deploys: ask once, then remember the answer
  # (even an empty one) so redeploys stay non-interactive.
  if ! ssh "$SERVER" "grep -q '^REDIRECT_DOMAIN=' omnianote/.env"; then
    read -r -p "    Extra domain to redirect to $DOMAIN, e.g. its bare apex (Enter to skip): " REDIRECT_DOMAIN
    ssh "$SERVER" "printf 'REDIRECT_DOMAIN=%s\n' '$REDIRECT_DOMAIN' >> omnianote/.env"
  fi
else
  echo "==> Creating server .env"
  prompt_username
  prompt_password
  read -r -p "    Anthropic API key (Enter to skip, add later in Settings): " ANTHROPIC_KEY
  read -r -p "    Backup git remote URL with token (Enter to skip): " BACKUP_REMOTE
  read -r -p "    Extra domain to redirect to $DOMAIN, e.g. its bare apex (Enter to skip): " REDIRECT_DOMAIN
  SESSION_SECRET="$(openssl rand -hex 32)"

  ssh "$SERVER" "cat > omnianote/.env && chmod 600 omnianote/.env" <<ENV
OMNIA_AUTH_USERNAME=$APP_USERNAME
OMNIA_AUTH_PASSWORD=$APP_PASSWORD
OMNIA_SESSION_SECRET=$SESSION_SECRET
DOMAIN=$DOMAIN
REDIRECT_DOMAIN=$REDIRECT_DOMAIN
OMNIA_ANTHROPIC_API_KEY=$ANTHROPIC_KEY
OMNIA_BACKUP_REMOTE=$BACKUP_REMOTE
OMNIA_BACKUP_INTERVAL=3600
ENV
fi

# The app would refuse to boot on these (OMNIA_REQUIRE_AUTH); stop now, while
# the running container is still untouched.
if ! ssh "$SERVER" 'bash -s' <<'REMOTE'
cd omnianote
grep -qx 'OMNIA_REQUIRE_AUTH=0' .env && exit 0
grep -q '^OMNIA_AUTH_PASSWORD=.' .env \
  && grep -q '^OMNIA_SESSION_SECRET=.' .env \
  && ! grep -qx 'OMNIA_SESSION_SECRET=change-me-in-production' .env
REMOTE
then
  echo "==> The server's omnianote/.env has an empty OMNIA_AUTH_PASSWORD or no real" >&2
  echo "    OMNIA_SESSION_SECRET, and the app refuses to start that way. Fix it and rerun." >&2
  exit 1
fi

# --- optional vault migration (first deploy only) --------------------------
# The vault goes into the volume before the app first boots. A booted app has
# already seeded Welcome.md and the templates and committed them, and copying
# over that brings back any of them the imported vault had deleted.
if [[ -n "$WITH_VAULT" && -d "$HERE/data/vault" ]]; then
  if ssh "$SERVER" "docker volume inspect omnianote_omnianote-data >/dev/null 2>&1"; then
    echo "==> Server data volume already exists — NOT copying local vault (avoid overwrite)."
    echo "    To migrate anyway, move the server vault aside manually first."
  else
    echo "==> Copying local vault into a new server volume (builds the image first)"
    rsync -az --delete "$HERE/data/vault/" "$SERVER:omnianote/vault-import/"
    # `up --no-start` creates the volume as compose's own (one made by a plain
    # docker run draws a compose warning on every later up) and starts nothing.
    ssh "$SERVER" "cd omnianote && export $TAGGED"' \
      && docker compose up --no-start app \
      && docker compose run --rm --no-deps -T -v "$PWD/vault-import:/src:ro" \
           --entrypoint sh app -c "mkdir -p /data/vault && cp -a /src/. /data/vault/" \
      && rm -rf vault-import'
  fi
fi

PULL=""
if [[ -n "$REFRESH" ]]; then
  PULL="--pull"
fi
echo "==> Building $REV${REFRESH:+ on freshly pulled base images} (first build takes a few minutes)"
if ! ssh "$SERVER" "cd omnianote && $TAGGED docker compose build $PULL app"; then
  echo "==> Build failed; the running app was not touched." >&2
  exit 1
fi
if [[ -n "$REFRESH" ]]; then
  # `up` below recreates caddy if its tag now names a newer image.
  echo "==> Pulling caddy's pinned tag again"
  if ! ssh "$SERVER" "cd omnianote && docker compose pull caddy"; then
    echo "==> Pulling caddy failed; the running app was not touched." >&2
    exit 1
  fi
fi

# `up` checks every image before it creates any container, and by then the
# chown step below may have stopped the app. Settle the rest of what `up`
# needs while the running app is untouched: a compose file this Compose
# rejects, or an image that will not pull (caddy's tag after a bump), ends
# the deploy here.
echo "==> Checking the compose file and its images"
if ! ssh "$SERVER" "bash -s -- $REV" <<'REMOTE'
set -euo pipefail
cd omnianote
export APP_TAG="$1" GIT_SHA="$1"
docker compose config -q
images="$(docker compose config --images)"
for image in $images; do
  docker image inspect "$image" >/dev/null 2>&1 || docker pull "$image"
done
REMOTE
then
  echo "==> The compose file or an image it names is not ready; the running app was not touched." >&2
  exit 1
fi

echo "==> Starting $REV"
# What runs now becomes the rollback target. First deploy: nothing to keep.
ssh "$SERVER" 'docker tag omnianote-app:latest omnianote-app:prev 2>/dev/null || true'

# The app runs as uid 10001 (Dockerfile). A volume from before that, or one a
# restore or a rollback to such an image wrote to, holds files it cannot
# write. Hand them over with the app stopped, so nothing root-owned appears
# between the chown and the new container. Once all of /data is 10001's this
# only walks the tree. A fresh volume is created by `up` below, already 10001's.
ssh "$SERVER" "bash -s -- $REV" <<'REMOTE' || fail "could not hand the data volume to uid 10001"
set -euo pipefail
REV="$1"
cd omnianote
docker volume inspect omnianote_omnianote-data >/dev/null 2>&1 || exit 0
# A plain docker run of the new image as root, with Docker's default
# capabilities: the compose service drops CAP_CHOWN.
in_volume() {
  docker run --rm --user 0 -v omnianote_omnianote-data:/data \
    --entrypoint "$1" "omnianote-app:$REV" "${@:2}"
}
not_ours="$(in_volume find /data ! -uid 10001 -print -quit)"
if [ -n "$not_ours" ]; then
  echo "    $not_ours (and maybe more) is not uid 10001's: stopping the app to chown /data"
  docker compose stop app
  in_volume chown -R 10001:10001 /data
fi
REMOTE
# --wait holds until the app's healthcheck passes; the timeout outlasts the
# healthcheck's 180s start_period.
ssh "$SERVER" "cd omnianote && $TAGGED docker compose up -d --no-build --wait --wait-timeout 240" \
  || fail "$REV did not turn healthy"

if [[ -n "$CADDY_BEFORE" && "$CADDY_BEFORE" != "$CADDY_AFTER" ]]; then
  echo "==> Caddyfile changed; recreating caddy so it loads the new config"
  ssh "$SERVER" "cd omnianote && $TAGGED docker compose up -d --no-deps --force-recreate caddy" \
    || fail "caddy did not restart with the new Caddyfile"
fi

# Plain `docker compose` (backup unit, RESTORE.md, .env edits) runs :latest.
ssh "$SERVER" "docker tag omnianote-app:$REV omnianote-app:latest" \
  || fail "could not tag $REV as latest"

echo "==> Checking https://$DOMAIN serves $REV"
verify_live || fail "https://$DOMAIN is not serving $REV correctly"

# What a later failed deploy's rollback comes back to.
ssh "$SERVER" 'cd omnianote && cp Caddyfile Caddyfile.verified && cp docker-compose.yml docker-compose.yml.verified' \
  || echo "==> WARNING: could not record this config as verified; a rollback would use an older one." >&2

# Every deploy leaves a tagged image behind; keep latest, prev and this one.
ssh "$SERVER" "bash -s -- $REV" <<'REMOTE' || true
docker images omnianote-app --format '{{.Tag}}' \
  | grep -vxE "latest|prev|<none>|$1" \
  | sed 's/^/omnianote-app:/' | xargs -r docker rmi >/dev/null 2>&1
docker image prune -f --filter until=168h >/dev/null
REMOTE

echo
echo "Done. https://$DOMAIN runs $REV (allow ~1 min on a first deploy for the"
echo "embedding-model download). Log in with the username and password you chose."
echo "Settings → Backup has a 'Push now' button to verify the backup remote."
echo "Redeploy after code changes: commit, then rerun this script (no --with-vault needed)."
echo "Back to the previous build: ./deploy.sh $SERVER $DOMAIN --rollback"
