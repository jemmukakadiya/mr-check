#!/usr/bin/env bash
# Installs the Claude routine files into ~/.claude/... on a fresh machine.
# Idempotent — safe to re-run.
#
# Usage:
#   cd "/Users/jemmu/Work/python scripts/mr check/setup"
#   bash install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SKILL_SRC="$SCRIPT_DIR/SKILL.md"
SKILL_DST_DIR="$HOME/.claude/scheduled-tasks/koinon-daily-mr-review-team-jemish"
SKILL_DST="$SKILL_DST_DIR/SKILL.md"

MEMO_SRC="$SCRIPT_DIR/feedback_mr_review_pdf.md"
MEMO_DST_DIR="$HOME/.claude/projects/-Users-jemmu-Work-Koinon/memory"
MEMO_DST="$MEMO_DST_DIR/feedback_mr_review_pdf.md"

RUNTIME_ROOT="$HOME/.koinon-mr-reviews"
PDF_DIR="$HOME/mr reviews"

echo "→ Installing scheduled task SKILL.md"
mkdir -p "$SKILL_DST_DIR"
cp "$SKILL_SRC" "$SKILL_DST"
echo "   $SKILL_DST"

echo "→ Installing PDF format memory"
mkdir -p "$MEMO_DST_DIR"
cp "$MEMO_SRC" "$MEMO_DST"
echo "   $MEMO_DST"

echo "→ Creating runtime directories"
mkdir -p "$RUNTIME_ROOT/pending" "$RUNTIME_ROOT/logs"
if [ ! -f "$RUNTIME_ROOT/state.json" ]; then
  echo '{}' > "$RUNTIME_ROOT/state.json"
  echo "   $RUNTIME_ROOT/state.json (created empty)"
else
  echo "   $RUNTIME_ROOT/state.json (already exists — left alone)"
fi
mkdir -p "$PDF_DIR"
echo "   $PDF_DIR"

ENV_FILE="$(cd "$SCRIPT_DIR/.." && pwd)/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo
  echo "!  No .env at $ENV_FILE"
  echo "   Run:   cp '$(cd "$SCRIPT_DIR/.." && pwd)/.env.example' '$ENV_FILE' && chmod 600 '$ENV_FILE'"
  echo "   Then edit it and paste your GITLAB_TOKEN."
fi

echo
echo "Done. Next:"
echo "  1. If you didn't set up .env yet, do that now."
echo "  2. Open Claude Code and register the cron with the schedule skill:"
echo "       cron '0 9-21/2 * * *' → $SKILL_DST"
echo "  3. Smoke test:  python3 '$(cd "$SCRIPT_DIR/.." && pwd)/fetch_mrs.py'"
