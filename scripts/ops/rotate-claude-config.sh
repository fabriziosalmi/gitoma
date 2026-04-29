#!/usr/bin/env bash
#
# rotate-claude-config.sh — rotate the Anthropic API key in your
# Claude Code config (.claude/settings.json or ~/.claude/settings.json)
#
# What it does
#   1. Locates your Claude Code settings.json (project-local OR user-home)
#   2. Opens the Anthropic Console API keys page in your browser
#   3. Prompts you to paste the NEW key
#   4. Tests the new key with a 1-token /v1/messages call
#   5. Backs up settings.json to settings.json.bak.<timestamp>
#   6. Updates the API key field in-place
#   7. Reminds you to revoke the old key in the Anthropic Console
#
# Usage
#   ./scripts/ops/rotate-claude-config.sh [path/to/settings.json]
#
# If no path passed, tries (in order):
#   ./.claude/settings.json
#   ~/.claude/settings.json
#   ~/.config/claude-code/settings.json

set -euo pipefail

ANTHROPIC_KEYS_PAGE="https://console.anthropic.com/settings/keys"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
warn() { printf "\033[33m%s\033[0m\n" "$*"; }
err()  { printf "\033[31m%s\033[0m\n" "$*" >&2; }
ok()   { printf "\033[32m%s\033[0m\n" "$*"; }

# Locate settings.json
SETTINGS=""
if [[ $# -ge 1 ]]; then
  SETTINGS="$1"
elif [[ -f "./.claude/settings.json" ]]; then
  SETTINGS="./.claude/settings.json"
elif [[ -f "$HOME/.claude/settings.json" ]]; then
  SETTINGS="$HOME/.claude/settings.json"
elif [[ -f "$HOME/.config/claude-code/settings.json" ]]; then
  SETTINGS="$HOME/.config/claude-code/settings.json"
else
  err "Could not locate Claude Code settings.json."
  err "Pass the path as the first argument."
  exit 1
fi

bold "→ Claude Code config rotation"
echo "  Settings file: $SETTINGS"
echo

if ! command -v jq >/dev/null 2>&1; then
  err "jq required (brew install jq)"
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  err "curl required"
  exit 1
fi

# Show which fields look like they might hold a key
bold "Fields in $SETTINGS that look like API key candidates:"
jq -r 'paths(scalars) as $p | select($p | join(".") | test("(?i)key|token|secret")) | $p | join(".")' "$SETTINGS" | sed 's/^/  /'
echo

read -rp "  Enter the JSON path to update (e.g. anthropic.api_key, env.ANTHROPIC_API_KEY): " KEY_PATH
if [[ -z "$KEY_PATH" ]]; then
  err "Empty path — aborted."
  exit 1
fi

# Verify the path resolves to a string
if ! jq -e --arg p "$KEY_PATH" 'getpath($p|split(".")) | type=="string"' "$SETTINGS" >/dev/null 2>&1; then
  warn "Path '$KEY_PATH' doesn't currently hold a string value."
  read -rp "  Continue anyway (will create the field)? [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] || { err "Aborted."; exit 1; }
fi
echo

bold "Step 1/4  Open the Anthropic Console keys page"
echo "  URL: $ANTHROPIC_KEYS_PAGE"
read -rp "  Press ENTER to open in your default browser… " _
open "$ANTHROPIC_KEYS_PAGE" 2>/dev/null || warn "  (couldn't auto-open)"
echo

bold "Step 2/4  Paste the new API key"
echo "  (Format: sk-ant-… — input is hidden)"
read -rsp "  New key: " NEW_KEY
echo
echo

if [[ -z "$NEW_KEY" ]]; then
  err "Empty key — aborting."
  exit 1
fi
if [[ ! "$NEW_KEY" =~ ^sk-ant- ]]; then
  warn "  Key doesn't start with sk-ant- — looks unusual."
  read -rp "  Continue anyway? [y/N] " yn
  [[ "$yn" =~ ^[Yy]$ ]] || { err "Aborted."; exit 1; }
fi

bold "Step 3/4  Test the new key (1-token /v1/messages call)"
HTTP_CODE=$(curl -s -o /tmp/anthropic-rotate.out -w "%{http_code}" \
  https://api.anthropic.com/v1/messages \
  -H "x-api-key: $NEW_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}' || echo "000")

if [[ "$HTTP_CODE" == "200" ]]; then
  ok "  ✓ Key works (HTTP 200 from /v1/messages)"
else
  err "  ✗ Test failed (HTTP $HTTP_CODE)"
  err "    Response (truncated):"
  head -c 400 /tmp/anthropic-rotate.out >&2
  echo
  err "    Aborted. Settings file NOT modified."
  rm -f /tmp/anthropic-rotate.out
  exit 1
fi
rm -f /tmp/anthropic-rotate.out
echo

bold "Step 4/4  Update settings.json (with timestamped backup)"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="${SETTINGS}.bak.${TS}"
cp -p "$SETTINGS" "$BACKUP"
ok "  ✓ Backup created: $BACKUP"

# In-place update via jq, writing to a tmp file then mv
TMP="$(mktemp)"
jq --arg p "$KEY_PATH" --arg v "$NEW_KEY" \
   'setpath($p|split("."); $v)' "$SETTINGS" > "$TMP"
mv "$TMP" "$SETTINGS"
# Preserve permissions
chmod 600 "$SETTINGS" 2>/dev/null || true
ok "  ✓ Updated $KEY_PATH in $SETTINGS"
echo

warn "  Now revoke the OLD key in the Anthropic Console:"
echo "    $ANTHROPIC_KEYS_PAGE"
echo
echo "  Backup kept at: $BACKUP   (delete after verifying Claude Code still works)"
ok "✓ Rotation complete."
