#!/usr/bin/env bash
#
# audit-secrets.sh — quick trivy scan of common secret-leak hot spots
#
# What it does
#   1. Runs trivy in secret-only mode against the cwd
#   2. Adds the ~/.claude / ~/.anthropic / .env / .env.* files explicitly
#      (trivy's filesystem scanner respects .gitignore — these are
#      gitignored by default so they would otherwise be skipped)
#   3. Renders a one-line-per-finding summary so you can see at a glance
#      whether anything has slipped in
#
# Usage
#   ./scripts/ops/audit-secrets.sh [path]
#
# Defaults to cwd. Exit code 0 if zero findings, 1 if any.

set -euo pipefail

TARGET="${1:-.}"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
warn() { printf "\033[33m%s\033[0m\n" "$*"; }
err()  { printf "\033[31m%s\033[0m\n" "$*" >&2; }
ok()   { printf "\033[32m%s\033[0m\n" "$*"; }

if ! command -v trivy >/dev/null 2>&1; then
  err "trivy required (brew install trivy)"
  exit 2
fi
if ! command -v jq >/dev/null 2>&1; then
  err "jq required (brew install jq)"
  exit 2
fi

bold "→ Secret audit on: $TARGET"
echo

# Repo-wide scan (respects .gitignore by default)
TMP_OUT="$(mktemp)"
trivy fs --quiet --scanners secret --format json \
  --severity HIGH,CRITICAL,MEDIUM \
  --exit-code 0 "$TARGET" > "$TMP_OUT"

# Also force-scan the gitignored secret-bearing files explicitly so
# they show up regardless of .gitignore. Aggregate into a second JSON.
EXTRA_FILES=()
[[ -f "$TARGET/.env" ]] && EXTRA_FILES+=("$TARGET/.env")
for f in "$TARGET/.env."*; do
  [[ -f "$f" && "$f" != *.bak.* ]] && EXTRA_FILES+=("$f")
done
[[ -f "$TARGET/.claude/settings.json" ]] && EXTRA_FILES+=("$TARGET/.claude/settings.json")
[[ -f "$HOME/.claude/settings.json" ]] && EXTRA_FILES+=("$HOME/.claude/settings.json")
[[ -f "$HOME/.config/claude-code/settings.json" ]] && EXTRA_FILES+=("$HOME/.config/claude-code/settings.json")

EXTRA_OUT="$(mktemp)"
echo '{"Results":[]}' > "$EXTRA_OUT"
for f in "${EXTRA_FILES[@]}"; do
  TMP_F="$(mktemp)"
  trivy fs --quiet --scanners secret --format json \
    --severity HIGH,CRITICAL,MEDIUM \
    --exit-code 0 "$f" > "$TMP_F" 2>/dev/null || true
  # Merge .Results
  jq -s '{"Results": ((.[0].Results // []) + (.[1].Results // []))}' \
     "$EXTRA_OUT" "$TMP_F" > "${EXTRA_OUT}.merged"
  mv "${EXTRA_OUT}.merged" "$EXTRA_OUT"
  rm -f "$TMP_F"
done

# Combined summary
TOTAL=$(jq -s '
  ([.[0].Results // []] | flatten | map(.Secrets // []) | flatten | length)
  + ([.[1].Results // []] | flatten | map(.Secrets // []) | flatten | length)
' "$TMP_OUT" "$EXTRA_OUT")

echo "  Repo-wide findings: $(jq '[.Results // [] | .[] | .Secrets // [] | .[]] | length' "$TMP_OUT")"
echo "  Extra-files findings: $(jq '[.Results // [] | .[] | .Secrets // [] | .[]] | length' "$EXTRA_OUT")"
echo "  TOTAL: $TOTAL"
echo

if [[ "$TOTAL" -eq 0 ]]; then
  ok "✓ No secrets detected."
  rm -f "$TMP_OUT" "$EXTRA_OUT"
  exit 0
fi

bold "Findings:"
for src in "$TMP_OUT" "$EXTRA_OUT"; do
  jq -r '
    .Results[]? as $r |
    ($r.Secrets // [])[] |
    "  - \($r.Target):\(.StartLine // 0)  \(.Severity)  \(.RuleID // .Category // "?")  \(.Title // "")"
  ' "$src"
done
echo

warn "  Rotate any unexpected leaks immediately."
warn "  For .env / .claude/settings.json: ./scripts/ops/rotate-*.sh"
rm -f "$TMP_OUT" "$EXTRA_OUT"
exit 1
