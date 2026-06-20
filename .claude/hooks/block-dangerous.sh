#!/usr/bin/env bash
# .claude/hooks/block-dangerous.sh
# MadCP PreToolUse safety hook — blocks dangerous Bash tool calls.
#
# Input:  JSON on stdin (Claude Code hook payload)
# Exit 0: allow
# Exit 2: block (message on stderr shown to Claude as tool result)

set -uo pipefail

INPUT=$(cat)

# Extract command — handle both 'tool_input' (current SDK) and 'input' (older SDK).
COMMAND=$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    cmd = (
        d.get('tool_input', {}).get('command')
        or d.get('input', {}).get('command')
        or ''
    )
    print(cmd)
except Exception:
    print('')
" 2>/dev/null || true)

# Nothing to check — not a bash call or payload unreadable.
[ -z "$COMMAND" ] && exit 0

block() {
    echo "BLOCKED by MadCP safety hook: $1" >&2
    exit 2
}

# ── Rule 1: destructive rm of / or ~ ──────────────────────────────────────────
# Matches rm with any combination of -r/-R and -f flags targeting / or ~.
if echo "$COMMAND" | grep -qE 'rm\s+(-[a-zA-Z]*[rR][a-zA-Z]*[fF][a-zA-Z]*|-[a-zA-Z]*[fF][a-zA-Z]*[rR][a-zA-Z]*|-[rR]\s+-[fF]|-[fF]\s+-[rR])\s+(\/|~\/?)\s*($|[;&|])'; then
    block "rm -rf / or rm -rf ~ detected"
fi
# Belt-and-suspenders: literal 'rm -rf /' anywhere in the command.
if echo "$COMMAND" | grep -qE '\brm\b.*\-rf\s+\/'; then
    block "rm -rf / detected"
fi

# ── Rule 2: writes to .env or credential-named files ──────────────────────────
# Targets: redirects (> >>), tee, cp/mv where destination matches.
if echo "$COMMAND" | grep -qiE '(>|>>|tee\b)\s*[^;|&]*\.env(\s|$|;|&&|\|\|)'; then
    block "write to .env file"
fi
if echo "$COMMAND" | grep -qiE '(>|>>|tee\b)\s*[^;|&]*credentials[^;|&]*'; then
    block "write to credentials file"
fi
# cp/mv to .env or credentials destination (last non-flag arg).
if echo "$COMMAND" | grep -qiE '\b(cp|mv)\b[^;|&]+\.env(\s|$)'; then
    block "cp/mv to .env file"
fi
if echo "$COMMAND" | grep -qiE '\b(cp|mv)\b[^;|&]+credentials'; then
    block "cp/mv to credentials file"
fi

# ── Rule 3: curl/wget to non-localhost ────────────────────────────────────────
if echo "$COMMAND" | grep -qE '\b(curl|wget)\b'; then
    # Extract all http(s) URLs from the command.
    URLS=$(echo "$COMMAND" | grep -oE 'https?://[^[:space:]"'"'"'>|&;]+' || true)
    if [ -n "$URLS" ]; then
        while IFS= read -r url; do
            [ -z "$url" ] && continue
            if ! echo "$url" | grep -qE '^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?(/|$)'; then
                block "curl/wget to non-localhost URL: $url"
            fi
        done <<< "$URLS"
    fi
fi

# ── Rule 4: absolute write targets outside project ────────────────────────────
# Determine project root from this script's location (.claude/hooks/ → ../..)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Extract destinations from redirect operators and tee.
WRITE_TARGETS=$(echo "$COMMAND" | grep -oE '(>>?|tee)\s+/[^[:space:];|&]+' \
    | grep -oE '/[^[:space:];|&]+' || true)

if [ -n "$WRITE_TARGETS" ]; then
    while IFS= read -r target; do
        [ -z "$target" ] && continue
        case "$target" in
            "$PROJECT_ROOT"/*|/tmp/*|/var/tmp/*|/dev/null|/dev/stderr|/dev/stdout)
                # Inside project or safe system paths — allow.
                ;;
            *)
                block "write to path outside project: $target"
                ;;
        esac
    done <<< "$WRITE_TARGETS"
fi

exit 0
