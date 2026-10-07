#!/usr/bin/env bash
# Refresh the public mirror (github.com/saetyrn/exscalia) from this repo's HEAD.
#
#   MIRROR_AUTHOR_EMAIL=<id>+<user>@users.noreply.github.com \
#     scripts/export-mirror.sh <mirror-clone-dir> [--dry-run]
#
# The mirror is one parentless commit, never this repo's history (commit
# authors here carry machine hostnames). `git archive HEAD` leaves out
# HANDOFF.md, AUDIT-*.md and .Rhistory by construction (.gitattributes
# export-ignore); the archive is then checked for private files and leaked
# identifiers and the run aborts on any hit. Only a clean archive replaces the
# mirror clone's tree, is committed under the noreply identity and is
# force-pushed. --dry-run stops before the push and leaves the snapshot commit
# in the clone for inspection; the next run replaces it.
set -euo pipefail

USAGE="usage: scripts/export-mirror.sh <mirror-clone-dir> [--dry-run]"
MIRROR=""
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -*) echo "$USAGE" >&2; exit 2 ;;
    *) [[ -z "$MIRROR" ]] || { echo "$USAGE" >&2; exit 2; }; MIRROR="$arg" ;;
  esac
done
[[ -n "$MIRROR" ]] || { echo "$USAGE" >&2; exit 2; }

PLACEHOLDER_EMAIL="ID+USER@users.noreply.github.com"
AUTHOR_NAME="${MIRROR_AUTHOR_NAME:-saetyrn}"
AUTHOR_EMAIL="${MIRROR_AUTHOR_EMAIL:-$PLACEHOLDER_EMAIL}"
if [[ "$AUTHOR_EMAIL" == "$PLACEHOLDER_EMAIL" || "$AUTHOR_EMAIL" != *@users.noreply.github.com ]]; then
  echo "refusing: set MIRROR_AUTHOR_EMAIL to the GitHub noreply address (<id>+<user>@users.noreply.github.com)" >&2
  exit 2
fi

REPO="$(cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)" && pwd -P)"
if [[ -n "$(git -C "$REPO" status --porcelain)" ]]; then
  echo "refusing: $REPO has uncommitted changes (the mirror is built from HEAD)" >&2
  git -C "$REPO" status --short >&2
  exit 1
fi

[[ -d "$MIRROR" ]] || { echo "refusing: no such directory: $MIRROR" >&2; exit 1; }
MIRROR="$(cd "$MIRROR" && pwd -P)"
if [[ "$(git -C "$MIRROR" rev-parse --show-toplevel 2>/dev/null || true)" != "$MIRROR" ]]; then
  echo "refusing: $MIRROR is not the root of a git clone" >&2
  exit 1
fi
common_dir() { (cd "$1" && cd "$(git rev-parse --git-common-dir)" && pwd -P); }
if [[ "$(common_dir "$MIRROR")" == "$(common_dir "$REPO")" ]]; then
  echo "refusing: $MIRROR is a checkout of this repo, not the mirror" >&2
  exit 1
fi
BRANCH="$(git -C "$MIRROR" symbolic-ref --short HEAD 2>/dev/null)" \
  || { echo "refusing: $MIRROR has a detached HEAD" >&2; exit 1; }
MIRROR_URL="$(git -C "$MIRROR" remote get-url origin 2>/dev/null)" \
  || { echo "refusing: $MIRROR has no origin remote" >&2; exit 1; }
if [[ "$MIRROR_URL" == "$(git -C "$REPO" remote get-url origin 2>/dev/null || true)" ]]; then
  echo "refusing: $MIRROR's origin is this repo's origin ($MIRROR_URL)" >&2
  exit 1
fi

HEAD_SHA="$(git -C "$REPO" rev-parse --short HEAD)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
echo "==> Archiving $HEAD_SHA"
git -C "$REPO" archive --format=tar HEAD | tar -x -C "$STAGE"

# Any IPv4 literal outside loopback, 0.0.0.0, the private ranges and the
# documentation ranges (RFC 5737) is a leak; so are home-directory paths,
# campus hostnames, device UDIDs (Apple's 8-16 hex shape) and ssh login
# targets (an uppercase placeholder like deploy.sh's usage line passes).
# The bracket expressions keep this file from matching its own patterns.
IPV4='([0-9]{1,3}[.]){3}[0-9]{1,3}'
IPV4_OK='(127|10)([.][0-9]{1,3}){3}|0[.]0[.]0[.]0|192[.]168([.][0-9]{1,3}){2}|172[.](1[6-9]|2[0-9]|3[01])([.][0-9]{1,3}){2}|(192[.]0[.]2|198[.]51[.]100|203[.]0[.]113)[.][0-9]{1,3}'
LEAKS="$(cd "$STAGE" && {
  find . \( -name HANDOFF.md -o -name AGENTS.md -o -name CLAUDE.md -o -name 'AUDIT-*.md' \) -print | sed 's/$/: private file in the archive/'
  grep -rInoE "$IPV4" . | grep -vE ":($IPV4_OK)\$" || true
  grep -rInE -e '/Users[/]' -e 'paws[.]uga' -e 'root[@][a-z0-9]' -e '[0-9A-F]{8}-[0-9A-F]{16}' . || true
})"
if [[ -n "$LEAKS" ]]; then
  echo "refusing: the archive of $HEAD_SHA leaks private data:" >&2
  printf '%s\n' "$LEAKS" | cut -c1-240 >&2
  exit 1
fi
echo "==> Denylist clean"

echo "==> Replacing the tree of $MIRROR ($BRANCH)"
rsync -a --delete --exclude .git "$STAGE/" "$MIRROR/"
# The work tree is now exactly the archive, so --force also takes files the
# archive's own .gitignore would skip.
git -C "$MIRROR" add --all --force .
TREE="$(git -C "$MIRROR" write-tree)"
if git -C "$MIRROR" rev-parse -q --verify HEAD >/dev/null \
  && ! git -C "$MIRROR" rev-parse -q --verify HEAD^ >/dev/null \
  && [[ "$(git -C "$MIRROR" rev-parse HEAD^{tree})" == "$TREE" ]] \
  && [[ "$(git -C "$MIRROR" log -1 --format='%ae %ce')" == "$AUTHOR_EMAIL $AUTHOR_EMAIL" ]]; then
  COMMIT="$(git -C "$MIRROR" rev-parse HEAD)"
  echo "==> Mirror snapshot already matches $HEAD_SHA ($COMMIT)"
else
  git -C "$MIRROR" diff --cached --shortstat HEAD 2>/dev/null || true
  COMMIT="$(GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
    GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
    git -C "$MIRROR" commit-tree "$TREE" -m "EXSCALIA snapshot $(date -u +%Y-%m-%d)")"
  git -C "$MIRROR" update-ref -m "export-mirror: snapshot of $HEAD_SHA" "refs/heads/$BRANCH" "$COMMIT"
  echo "==> Committed $COMMIT (parentless, $AUTHOR_NAME <$AUTHOR_EMAIL>)"
fi

PUSH=(git -C "$MIRROR" push --force origin "refs/heads/$BRANCH:refs/heads/$BRANCH")
if (( DRY_RUN )); then
  echo "==> Dry run, not pushing. The push would be:"
  printf '%q ' "${PUSH[@]}"; echo
  exit 0
fi
echo "==> Pushing to $MIRROR_URL:"
printf '%q ' "${PUSH[@]}"; echo
"${PUSH[@]}"
