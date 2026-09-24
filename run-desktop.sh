#!/usr/bin/env bash
# Launch Chess Opening Improvement Desktop App
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure local node, npm, and conda python are in PATH
export PATH="/home/robin/.local/bin:/home/robin/miniconda3/bin:$PATH"

# Run Electron
exec ./node_modules/.bin/electron . "$@"
