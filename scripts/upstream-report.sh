#!/usr/bin/env bash
# Report what an upstream Riven update would touch in this fork.
#
# This fork deliberately diverges (TPDB replaces TMDB, most providers are
# removed), so merges are reviewed, not automatic. What makes them cheap is
# knowing up front which of *our* modified files upstream has changed --
# usually a handful, often none.
#
# Usage:  ./scripts/upstream-report.sh [upstream-ref]
set -euo pipefail

UPSTREAM_URL="https://github.com/rivenmedia/riven.git"
REF="${1:-upstream/main}"

if ! git remote get-url upstream >/dev/null 2>&1; then
    echo "Adding 'upstream' remote -> $UPSTREAM_URL"
    git remote add upstream "$UPSTREAM_URL"
fi

echo "Fetching upstream..."
git fetch upstream --quiet

BASE="$(git merge-base HEAD "$REF")"

echo
echo "Fork base:        $(git log --oneline -1 "$BASE" | cut -c1-72)"
echo "Upstream head:    $(git log --oneline -1 "$REF" | cut -c1-72)"
echo "New upstream commits: $(git rev-list --count "$BASE..$REF")"
echo

# Files this fork has modified (not added, not deleted) are the only ones
# that can produce a real content conflict.
# Portable: macOS ships bash 3.2, which has no `mapfile`.
OURS="$(git diff --name-status "$BASE..HEAD" -- src/ | awk '$1=="M"{print $2}')"
OURS_COUNT="$(printf '%s\n' "$OURS" | grep -c . || true)"

echo "Fork-modified upstream files: $OURS_COUNT"
echo

CONFLICTS=""
for f in $OURS; do
    if ! git diff --quiet "$BASE..$REF" -- "$f" 2>/dev/null; then
        CONFLICTS="$CONFLICTS$f"$'\n'
    fi
done
CONFLICT_COUNT="$(printf '%s' "$CONFLICTS" | grep -c . || true)"

if [ "$CONFLICT_COUNT" -eq 0 ]; then
    echo "No upstream changes to any file this fork modified."
    echo "A merge should apply cleanly apart from deleted-file prompts."
else
    echo "NEEDS REVIEW -- upstream changed these files, which this fork also modified:"
    for f in $CONFLICTS; do
        ours=$(git diff --numstat "$BASE..HEAD" -- "$f" | awk '{print $1+$2}')
        theirs=$(git diff --numstat "$BASE..$REF" -- "$f" | awk '{print $1+$2}')
        printf "  %-52s ours:%-6s theirs:%s\n" "$f" "${ours:-0}" "${theirs:-0}"
    done
    echo
    echo "Review each with:  git diff $BASE..$REF -- <file>"
    echo "Our own change:    git diff $BASE..HEAD -- <file>"
fi

echo
echo "Deleted upstream files: $(git diff --name-status "$BASE..HEAD" -- src/ | grep -c '^D' || true)"
echo "  (mostly src/schemas/* for removed providers; on a modify/delete"
echo "   conflict the answer is almost always 'stay deleted')"
