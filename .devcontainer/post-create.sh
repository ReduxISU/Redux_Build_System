#!/usr/bin/env bash
set -euo pipefail

BIN="${HOME}/.local/bin"

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if ! command -v mise >/dev/null 2>&1; then
  curl -fsSL https://mise.run | sh
fi
grep -qF 'mise activate bash' "${HOME}/.bashrc" 2>/dev/null || echo "eval \"\$(${BIN}/mise activate bash)\"" >> "${HOME}/.bashrc"
"${BIN}/mise" trust

# Install the CLI itself so `rbs` is on PATH inside the container.
"${BIN}/uv" sync --all-groups
"${BIN}/uv" tool install --editable .
