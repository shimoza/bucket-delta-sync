#!/usr/bin/env bash
# Scan for key-shaped strings before they reach a commit.
# With no args: scans staged changes. With --all: scans the whole tree.
# Exit 1 if anything suspicious is found.
set -uo pipefail

MODE="${1:-staged}"

# Files we never flag (templates carry placeholders on purpose).
EXCLUDE_RE='(\.env\.example|config\.example\.toml|check-secrets\.sh|SECURITY\.md)$'

# Patterns that look like real secrets, one per line. In order:
# AWS access key id, AWS secret, Azure connection-string key, Azure account-key
# shape, SAS signature, GitHub token, PEM private key, obsutil inline ak+sk.
declare -a PATTERNS=(
  'AKIA[0-9A-Z]{16}'
  'aws_secret_access_key[[:space:]]*=[[:space:]]*[A-Za-z0-9/+]{30,}'
  'AccountKey=[A-Za-z0-9/+]{60,}=='
  '[A-Za-z0-9/+]{86,88}=='
  'sig=[A-Za-z0-9%]{40,}'
  'ghp_[A-Za-z0-9]{36}'
  'BEGIN[[:space:]]+[A-Z ]*PRIVATE KEY'
  '-i=[A-Za-z0-9]{20}[[:space:]]+-k='
)

if [ "$MODE" = "--all" ]; then
  FILES=$(git ls-files)
else
  FILES=$(git diff --cached --name-only --diff-filter=ACM)
fi

[ -z "$FILES" ] && { echo "check-secrets: nothing to scan"; exit 0; }

found=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  echo "$f" | grep -Eq "$EXCLUDE_RE" && continue
  for p in "${PATTERNS[@]}"; do
    hits=$(grep -nEI "$p" "$f" 2>/dev/null)
    if [ -n "$hits" ]; then
      echo "POSSIBLE SECRET in $f:"
      echo "$hits" | sed 's/^/    /'
      found=1
    fi
  done
done <<< "$FILES"

if [ "$found" -ne 0 ]; then
  echo ""
  echo "check-secrets: blocked. Remove the secret or move it to an env var."
  echo "If this is a false positive, review carefully before overriding."
  exit 1
fi

echo "check-secrets: clean"
exit 0
