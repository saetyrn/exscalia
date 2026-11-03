#!/usr/bin/env bash
# Refresh the public mirror: one parentless commit holding this repo's HEAD.
#
#   refresh-mirror.sh --mirror PATH --forbid STRING [--forbid STRING ...]
#   refresh-mirror.sh --dry-run [--mirror PATH] [--forbid STRING ...]
#   refresh-mirror.sh --help
#
# Exports `git archive HEAD` into a scratch directory. The root .gitattributes
# export-ignores HANDOFF.md, AUDIT-*.md, .Rhistory and .claude, and the export
# is refused if a HANDOFF.md or AUDIT-*.md is in it anyway or if any --forbid
# string occurs in any exported file. The mirror clone's tree is then replaced
# with the export and committed with no parent, as the mirror's own noreply
# author, so neither this repo's history nor its commit identities travel.
# Nothing is pushed: the force-push is printed for you to run.
#
#   --mirror PATH     a clean clone of the public mirror repository
#   --forbid STRING   refuse if STRING occurs in the export (repeatable)
#   --author 'Name <noreply-email>'
#                     the commit identity; defaults to the author of the
#                     mirror's current commit, and must be a noreply address
#   --dry-run         export and check only; with --mirror, also show what
#                     would change there (the mirror's files and refs are
#                     left alone)
#
# Forbid, every time, what must never be published: the VPS address, the
# iPad's UDID and the Apple team id (all three are in HANDOFF.md). Not the
# public domain: the desktop and iPad apps ship it as their default server.
# They are arguments rather than constants here because this script is itself
# exported.
set -euo pipefail

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
die() { echo "refresh-mirror: $*" >&2; exit 1; }

MIRROR=""
AUTHOR=""
DRY_RUN=""
FORBID=()
while (( $# )); do
  case "$1" in
    --mirror) [[ -n "${2:-}" ]] || die "--mirror needs a path"; MIRROR="$2"; shift 2 ;;
    --forbid) [[ -n "${2:-}" ]] || die "--forbid needs a non-empty string"; FORBID+=("$2"); shift 2 ;;
    --author) [[ -n "${2:-}" ]] || die "--author needs 'Name <email>'"; AUTHOR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ -z "$DRY_RUN" ]]; then
  [[ -n "$MIRROR" ]] || { usage >&2; die "--mirror is required unless --dry-run"; }
  (( ${#FORBID[@]} )) || die "pass the VPS address (and the UDID) with --forbid"
fi

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
REV="$(git -C "$REPO" rev-parse HEAD)"
if [[ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]]; then
  echo "refresh-mirror: uncommitted changes are NOT exported; only HEAD (${REV:0:7}) is." >&2
fi

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
EXPORT="$SCRATCH/export"
mkdir "$EXPORT"
git -C "$REPO" archive --format=tar HEAD | tar -x -C "$EXPORT"

failed=""
private="$(cd "$EXPORT" && find . \( -name HANDOFF.md -o -name AGENTS.md -o -name CLAUDE.md -o -name 'AUDIT-*.md' \) -print)"
if [[ -n "$private" ]]; then
  echo "refresh-mirror: private files are in the export (is .gitattributes committed?):" >&2
  printf '%s\n' "$private" | sed 's/^/    /' >&2
  failed=1
fi
n=0
for s in ${FORBID[@]+"${FORBID[@]}"}; do
  n=$((n + 1))
  rc=0
  hits="$(cd "$EXPORT" && grep -rlF -- "$s" .)" || rc=$?
  if (( rc > 1 )); then
    die "grep failed while checking forbidden string #$n"
  elif (( rc == 0 )); then
    # The string itself is not echoed: this output tends to end up in logs.
    echo "refresh-mirror: forbidden string #$n occurs in:" >&2
    printf '%s\n' "$hits" | sed 's/^/    /' >&2
    failed=1
  fi
done
[[ -z "$failed" ]] || die "refusing to publish this export"
files="$(cd "$EXPORT" && find . -type f -o -type l | wc -l | tr -d ' ')"
echo "refresh-mirror: export of ${REV:0:7} is clean ($files files, ${#FORBID[@]} forbidden string(s) checked)"
(( ${#FORBID[@]} )) || echo "refresh-mirror: no --forbid given, so only the HANDOFF/AUDIT check ran" >&2

if [[ -z "$MIRROR" ]]; then
  exit 0
fi

MIRROR="$(git -C "$MIRROR" rev-parse --show-toplevel)" || die "--mirror is not a git clone"
GIT_DIR_M="$(git -C "$MIRROR" rev-parse --absolute-git-dir)"
# A worktree or clone of this repo shares its history; committing over it
# would move a private branch onto the snapshot.
if git -C "$MIRROR" cat-file -e "$REV^{commit}" 2>/dev/null; then
  die "$MIRROR holds this repo's history; --mirror must be a clone of the public mirror"
fi
BRANCH="$(git -C "$MIRROR" symbolic-ref --quiet --short HEAD)" || die "the mirror clone is on a detached HEAD"
OLD="$(git -C "$MIRROR" rev-parse --verify --quiet 'HEAD^{commit}')" || die "the mirror clone has no commit yet"
[[ -z "$(git -C "$MIRROR" status --porcelain)" ]] || die "the mirror clone has local changes"

if [[ -z "$AUTHOR" ]]; then
  AUTHOR="$(git -C "$MIRROR" log -1 --format='%an <%ae>' "$OLD")"
fi
author_re='^(.+) <([^<>]+)>$'
[[ "$AUTHOR" =~ $author_re ]] || die "author '$AUTHOR' is not 'Name <email>'"
NAME="${BASH_REMATCH[1]}"
EMAIL="${BASH_REMATCH[2]}"
[[ "$EMAIL" == *noreply* ]] || die "author email $EMAIL is not a noreply address; pass a noreply --author"

# The tree comes from a throwaway index over the export, so the dry run shows
# exactly what the real run commits. --force: the export is the tracked tree,
# and an ignore rule must not silently drop a tracked file.
export GIT_INDEX_FILE="$SCRATCH/index"
git --git-dir="$GIT_DIR_M" --work-tree="$EXPORT" -C "$EXPORT" add --all --force .
TREE="$(git --git-dir="$GIT_DIR_M" write-tree)"
unset GIT_INDEX_FILE

if [[ "$TREE" == "$(git -C "$MIRROR" rev-parse "$OLD^{tree}")" ]]; then
  echo "refresh-mirror: $MIRROR already matches ${REV:0:7}; nothing to commit"
  exit 0
fi
echo "refresh-mirror: changes against the mirror's current commit:"
git -C "$MIRROR" --no-pager diff --stat=100 "$OLD" "$TREE" | tail -n 40

if [[ -n "$DRY_RUN" ]]; then
  echo "refresh-mirror: dry run; the files and refs of $MIRROR are unchanged"
  exit 0
fi

NEW="$(GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" \
  GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL" \
  git -C "$MIRROR" commit-tree --no-gpg-sign -m "EXSCALIA snapshot, $(date +%F)" "$TREE")"
# Index and files first, then the branch: an interruption in between leaves a
# state that `git checkout -f` repairs.
git -C "$MIRROR" read-tree -m -u "$OLD" "$NEW"
git -C "$MIRROR" update-ref -m "refresh-mirror: snapshot" "refs/heads/$BRANCH" "$NEW" "$OLD"

REMOTE="$(git -C "$MIRROR" config "branch.$BRANCH.remote" || echo origin)"
echo "refresh-mirror: committed ${NEW:0:7} on $BRANCH as $NAME <$EMAIL>"
echo "Review it (git -C \"$MIRROR\" show --stat), then publish with:"
echo "  git -C \"$MIRROR\" push --force $REMOTE $BRANCH"
