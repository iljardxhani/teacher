#!/usr/bin/env bash
set -euo pipefail

REPO_URL_DEFAULT="git@github.com:iljardxhani/teacher.git"
BRANCH_DEFAULT="main"

REPO_URL="${1:-$REPO_URL_DEFAULT}"
BRANCH="${2:-$BRANCH_DEFAULT}"
TARGET_DIR="${3:-teacher}"

print_usage() {
  cat <<USAGE
Usage:
  $(basename "$0") [repo_url] [branch] [target_dir]

Examples:
  $(basename "$0")
  $(basename "$0") git@github.com:iljardxhani/teacher.git main /opt/teacher
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  exit 0
fi

if ! command -v git >/dev/null 2>&1; then
  echo "[error] git is required." >&2
  exit 1
fi

if [[ -d .git ]]; then
  echo "[info] Using existing repo at $(pwd)"

  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "[error] Uncommitted changes found. Commit or stash first." >&2
    exit 2
  fi

  CURRENT_REMOTE="$(git remote get-url origin 2>/dev/null || true)"
  if [[ -z "$CURRENT_REMOTE" ]]; then
    git remote add origin "$REPO_URL"
    echo "[info] Added origin: $REPO_URL"
  fi

  git fetch origin "$BRANCH"
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
else
  echo "[info] Cloning $REPO_URL (branch: $BRANCH) into $TARGET_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
  cd "$TARGET_DIR"
fi

echo "[ok] Repo synced."
echo "[ok] HEAD: $(git rev-parse --short HEAD)"
git log --oneline -n 5
