#!/usr/bin/env bash
# Deploys the exact pushed commit into the existing /home/server/stkbot clone.
#
# Runs as the bot user on the server (same systemd --user scope as
# stkbot.service), so no SSH key or GitHub secret is required. The ignored
# .env and .venv in the deploy directory are never touched by git, and the
# bot unit file is never modified.
set -euo pipefail

DEPLOY_SHA="${1:?usage: deploy.sh <commit-sha>}"
DEPLOY_DIR="/home/server/stkbot"

fail() {
  echo "::error::$*" >&2
  exit 1
}

# Limited service diagnostics on failure; never print env vars or full logs.
diagnose_service() {
  systemctl --user status stkbot.service --no-pager --lines=15 2>&1 \
    | sed -n '1,30p' || true
}

command -v git >/dev/null 2>&1 || fail "git is not installed on the runner host"
[ -d "$DEPLOY_DIR" ] || fail "deploy directory $DEPLOY_DIR does not exist"
cd "$DEPLOY_DIR"

# Repository identity guard: only deploy from the expected origin.
expected_origin="https://github.com/sinisterchiller/stkbot.git"
actual_origin="$(git remote get-url origin)"
[ "$actual_origin" = "$expected_origin" ] \
  || fail "origin mismatch: expected $expected_origin, got $actual_origin"

# Abort on dirty TRACKED files (user changes win); untracked files are skipped.
dirty="$(git status --porcelain=v2 | grep -v '^?? ' || true)"
if [ -n "$dirty" ]; then
  fail "tracked files are modified in $DEPLOY_DIR; refusing to overwrite user changes:
$dirty"
fi

git fetch origin main

# The pushed SHA must exist and be an ancestor of origin/main (branch guard).
git cat-file -e "${DEPLOY_SHA}^{commit}" || fail "commit $DEPLOY_SHA not found after fetch"
git merge-base --is-ancestor "$DEPLOY_SHA" origin/main \
  || fail "commit $DEPLOY_SHA is not on origin/main"

# Detached checkout of the exact commit. .env/.venv are untracked/ignored,
# so git never touches them.
git checkout --detach "$DEPLOY_SHA"

# Install requirements into the persistent venv (no-op when satisfied).
"$DEPLOY_DIR/.venv/bin/pip" --quiet install -r requirements.txt \
  || fail "pip install -r requirements.txt failed"

# Tests must pass BEFORE the service is restarted.
PYTHONDONTWRITEBYTECODE=1 "$DEPLOY_DIR/.venv/bin/python" -m pytest -p no:cache -q test_bot.py \
  || { diagnose_service; fail "tests failed; stkbot.service was not restarted"; }

# Restart the user-level service (same systemd --user scope as the runner).
systemctl --user restart stkbot.service \
  || { diagnose_service; fail "systemctl --user restart stkbot.service failed"; }

# Verify: service active and clone HEAD equals the pushed SHA.
active="$(systemctl --user is-active stkbot.service)"
[ "$active" = "active" ] \
  || { diagnose_service; fail "stkbot.service is not active after restart (status: $active)"; }
head="$(git -C "$DEPLOY_DIR" rev-parse HEAD)"
[ "$head" = "$DEPLOY_SHA" ] \
  || fail "deploy HEAD $head does not match pushed commit $DEPLOY_SHA"

echo "Deployed $DEPLOY_SHA; stkbot.service active."
