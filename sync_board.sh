#!/usr/bin/env bash

set -e

# -----------------------------
# CONFIG (edit these)
# -----------------------------
LOCAL_DIR="."
REMOTE_USER="xilinx"
REMOTE_HOST="${1:-vlsi-rf4x2.polito.it}"
REMOTE_DIR="/home/xilinx/FIREQ-test"

# -----------------------------
# BUILD EXCLUDE LIST
# -----------------------------

EXCLUDES_FILE=$(mktemp)

# Convert .gitignore into rsync exclude patterns
if [ -f "$LOCAL_DIR/.gitignore" ]; then
    grep -v '^#' "$LOCAL_DIR/.gitignore" | grep -v '^$' > "$EXCLUDES_FILE"
fi

# Extra Python / IDE noise
cat <<EOF >> "$EXCLUDES_FILE"
__pycache__/
*.pyc
*.pyo
*.pyd
.vscode/
.idea/
.env/
.venv/
.git/
EOF

# -----------------------------
# RUN RSYNC
# -----------------------------

rsync -avz \
    --exclude-from="$EXCLUDES_FILE" \
    "$LOCAL_DIR/" \
    "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"

# -----------------------------
# CLEANUP
# -----------------------------
rm "$EXCLUDES_FILE"

echo "Sync complete → ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"