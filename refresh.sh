#!/usr/bin/env bash
# Refresh PR data and open the dashboard.
#
# Re-runs the fetcher (resumable — uses cache.json) then opens index.html.
# Safe to run repeatedly: completed past years are served from cache, only the
# current year and any year with an open PR are re-checked.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v gh >/dev/null 2>&1; then
  echo "error: GitHub CLI (gh) is not installed — see https://cli.github.com/" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "error: not logged in to GitHub. Run: gh auth login" >&2
  exit 1
fi

python3 fetch_prs.py

# Open in the default browser (macOS: open, Linux: xdg-open).
if command -v open >/dev/null 2>&1; then
  open index.html
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open index.html
else
  echo "Done. Open index.html in your browser."
fi
