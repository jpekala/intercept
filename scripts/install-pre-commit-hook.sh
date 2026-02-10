#!/bin/bash
# Setup script to install the pre-commit hook

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_DIR="$REPO_ROOT/.git/hooks"
SCRIPT_FILE="$REPO_ROOT/scripts/pre-commit-update-docker-version.sh"
HOOK_FILE="$HOOK_DIR/pre-commit"

# Create hooks directory if it doesn't exist
mkdir -p "$HOOK_DIR"

# Copy the pre-commit script to git hooks
cp "$SCRIPT_FILE" "$HOOK_FILE"
chmod +x "$HOOK_FILE"

echo "✓ Pre-commit hook installed successfully!"
echo "✓ The hook will validate VERSION from config.py on each commit"
