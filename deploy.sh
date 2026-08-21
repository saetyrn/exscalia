#!/usr/bin/env bash
# One-command EXSCALIA deployment.
#
#   ./deploy.sh root@YOUR_SERVER_IP notes.yourdomain.com [--with-vault]
#
# Run from the repo root on your local machine. Requires ssh access to an
# Ubuntu/Debian server (your VPS provider gives you this when you add your
# SSH key at creation). --with-vault also copies your local data/vault to the
# server (first deploy only — it will not overwrite an existing server vault).
#
# The script prompts for credentials (login username/password, optional AI keys, optional
# backup remote) directly in your terminal; they are written straight into
# the server's .env over ssh.
set -euo pipefail

SERVER="${1:-}"
DOMAIN="${2:-}"
WITH_VAULT="${3:-}"

if [[ -z "$SERVER" || -z "$DOMAIN" ]]; then
  echo "usage: ./deploy.sh user@server-ip your.domain.com [--with-vault]" >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REMOTE_DIR="~/omnianote"

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

echo "==> Checking ssh access to $SERVER"
ssh -o ConnectTimeout=10 "$SERVER" true

echo "==> Syncing app code to $SERVER:$REMOTE_DIR"
rsync -az --delete \
  --exclude data --exclude .env \
  --exclude frontend/node_modules --exclude frontend/build --exclude frontend/.svelte-kit \
  --exclude backend/.venv --exclude '**/__pycache__' \
  --exclude desktop --exclude updates \
  "$HERE/" "$SERVER:omnianote/"

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
  read -r -s -p "    Choose a login password for the web UI: " APP_PASSWORD; echo
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

# --- optional vault migration (first deploy only) --------------------------
if [[ "$WITH_VAULT" == "--with-vault" && -d "$HERE/data/vault" ]]; then
  if ssh "$SERVER" "docker volume inspect omnianote_omnianote-data >/dev/null 2>&1"; then
    echo "==> Server data volume already exists — NOT copying local vault (avoid overwrite)."
    echo "    To migrate anyway, move the server vault aside manually first."
  else
    echo "==> Copying local vault to server (will import after first boot)"
    rsync -az "$HERE/data/vault/" "$SERVER:omnianote/vault-import/"
  fi
fi

echo "==> Building and starting (first build takes a few minutes)"
ssh "$SERVER" "cd omnianote && docker compose up -d --build"

if ssh "$SERVER" test -d omnianote/vault-import; then
  echo "==> Importing your vault into the server volume"
  ssh "$SERVER" 'cd omnianote \
    && docker compose cp vault-import/. app:/data/vault/ \
    && docker compose restart app \
    && rm -rf vault-import'
fi

echo
echo "Done. Open https://$DOMAIN (allow ~1 min for the first HTTPS certificate"
echo "and the embedding-model download). Log in with the username and password you chose."
echo "Settings → Backup has a 'Push now' button to verify the backup remote."
echo "Redeploy after code changes: rerun this script (no --with-vault needed)."
