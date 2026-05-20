#!/usr/bin/env bash
# random_review.sh — create a review with a random score (1-10).
#
# Usage:
#   ./random_review.sh "portable vacuum cleaner"
#   ./random_review.sh "wireless earbuds" 5     # second arg = how many to create

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 \"ITEM\" [COUNT]" >&2
    exit 1
fi

ITEM="$1"
COUNT="${2:-1}"

for ((i = 1; i <= COUNT; i++)); do
    # $RANDOM is 0-32767; modulo 10 gives 0-9, plus 1 gives 1-10.
    SCORE=$(( RANDOM % 10 + 1 ))
    echo "[$i/$COUNT] Generating review for '$ITEM' with score $SCORE..."
    python review_agent.py --create "$SCORE" "$ITEM"
done
