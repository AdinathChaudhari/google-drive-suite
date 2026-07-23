#!/usr/bin/env bash
# Install drivecast's git hooks into this clone.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cp "$root/scripts/pre-commit" "$root/.git/hooks/pre-commit"
chmod +x "$root/.git/hooks/pre-commit"
echo "Installed pre-commit hook -> .git/hooks/pre-commit"
