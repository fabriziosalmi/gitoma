#!/usr/bin/env bash
#
# rotate-diary-token.sh — rotate the GITOMA_DIARY_TOKEN (GitHub fine-grained PAT)
#
# What it does
#   1. Opens the GitHub fine-grained PAT creation page in your browser
#      (with the right scopes pre-selected if the URL still supports it)
#   2. Prompts you to paste the NEW token
#   3. Tests the new token with `git ls-remote` against fabgpt-coder/log
#   4. Backs up the existing .env to .env.bak.<timestamp>
#   5. Updates .env in-place with the new token
#   6. Reminds you to MANUALLY revoke the old PAT in GitHub UI
#
# Usage
#   ./scripts/ops/rotate-diary-token.sh [path/to/.env]
#
# Defaults to .env in the gitoma repo root.

set -euo pipefail

ENV_FILE="${1:-$(dirname "$0")/../../.env}"
DIARY_REPO="${GITOMA_DIARY_REPO:-fabgpt-coder/log}"
PAT_PAGE="https://github.com/settings/personal-access-tokens/new"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
warn() { printf "\033[33m%s\033[0m\n" "$*"; }
err()  { printf "\033[31m%s\033[0m\n" "$*" >&2; }
ok()   { printf "\033[32m%s\033[0m\n" "$*"; }

if [[ ! -f "$ENV_FILE" ]]; then
  err "ENV file not found: $ENV_FILE"
  err "Pass a path as the first argument, or run from the gitoma repo root."
  exit 1
fi

bold "→ Diary token rotation"
echo "  ENV file:    $ENV_FILE"
echo "  Diary repo:  $DIARY_REPO"
echo

bold "Step 1/5  Open the GitHub fine-grained PAT page in your browser"
echo "  URL: $PAT_PAGE"
echo
echo "  Required scopes:"
echo "    Repository access: Only select repositories → $DIARY_REPO"
echo "    Permissions:       Contents → Read and write"
echo
read -rp "  Press ENTER to open the page in your default browser… " _
open "$PAT_PAGE" 2>/dev/null || warn "  (couldn't auto-open — visit the URL manually)"
echo

bold "Step 2/5  Paste the new PAT"
echo "  (Format: github_pat_… — input is hidden)"
read -rsp "  New PAT: " NEW_PAT
echo
echo

if [[ -z "$NEW_PAT" ]]; then
  err "Empty token — aborting."
  exit 1
fi
if [[ ! "$NEW_PAT" =~ ^(ghp_|github_pat_) ]]; then
  warn "  Token doesn't start with ghp_ or github_pat_ — this looks unusual."
  read -rp "  Continue anyway? [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] || { err "Aborted."; exit 1; }
fi

bold "Step 3/5  Test the new token against $DIARY_REPO"
TEST_URL="https://x-access-token:${NEW_PAT}@github.com/${DIARY_REPO}.git"
if git ls-remote --quiet "$TEST_URL" HEAD >/dev/null 2>&1; then
  ok "  ✓ Token works (ls-remote succeeded)"
else
  err "  ✗ ls-remote failed — token may not have the right scopes."
  err "    Aborted. The .env file has NOT been modified."
  exit 1
fi
echo

bold "Step 4/5  Update .env (with timestamped backup)"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="${ENV_FILE}.bak.${TS}"
cp -p "$ENV_FILE" "$BACKUP"
ok "  ✓ Backup created: $BACKUP"

# Replace the line; if absent, append.
if grep -q '^GITOMA_DIARY_TOKEN=' "$ENV_FILE"; then
  # Use a delimiter unlikely to appear in PATs
  awk -v new="$NEW_PAT" '
    /^GITOMA_DIARY_TOKEN=/ { print "GITOMA_DIARY_TOKEN=" new; next }
    { print }
  ' "$BACKUP" > "$ENV_FILE"
  ok "  ✓ Updated GITOMA_DIARY_TOKEN line in $ENV_FILE"
else
  printf "\nGITOMA_DIARY_TOKEN=%s\n" "$NEW_PAT" >> "$ENV_FILE"
  ok "  ✓ Appended GITOMA_DIARY_TOKEN to $ENV_FILE"
fi
echo

bold "Step 5/5  REVOKE THE OLD PAT (manual)"
warn "  GitHub doesn't expose a CLI to revoke fine-grained PATs."
echo "  Visit: https://github.com/settings/tokens?type=beta"
echo "  Find the OLD token in the list and click 'Delete'."
echo
read -rp "  Press ENTER once you've revoked the old PAT… " _
echo

ok "✓ Rotation complete."
echo "  Backup: $BACKUP   (delete after verifying gitoma still works)"
echo "  Test:   GITOMA_DIARY_REPO=$DIARY_REPO source $ENV_FILE && \\"
echo "          git ls-remote https://x-access-token:\$GITOMA_DIARY_TOKEN@github.com/$DIARY_REPO.git HEAD"
