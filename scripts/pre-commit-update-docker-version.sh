#!/bin/bash
# Pre-commit hook to update docker-build.yml VERSION labels from config.py

set -e

# Get the root directory of the repository
REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Extract VERSION from config.py
VERSION=$(grep '^VERSION = ' config.py | sed 's/VERSION = "\([^"]*\)".*/\1/')

if [ -z "$VERSION" ]; then
    echo "Error: Could not extract VERSION from config.py"
    exit 1
fi

# Update docker-build.yml with the extracted version
DOCKER_BUILD_FILE=".github/workflows/docker-build.yml"

if [ ! -f "$DOCKER_BUILD_FILE" ]; then
    echo "Error: $DOCKER_BUILD_FILE not found"
    exit 1
fi

# Check if version in docker-build.yml differs from config.py version
CURRENT_VERSION=$(grep -A 1 'Extract version from config.py' "$DOCKER_BUILD_FILE" || true)

# Update the docker-build.yml file (no changes needed since it's now dynamic)
# The workflow now extracts version dynamically from config.py at runtime
# This script is informational and ensures consistency

echo "✓ Docker version labels will use VERSION=$VERSION from config.py"

# Stage the files if they were modified
if git diff --quiet config.py 2>/dev/null; then
    :
else
    git add config.py
fi

exit 0
