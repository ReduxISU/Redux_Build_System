#!/usr/bin/env bash
set -euo pipefail

VERSION="${VERSION:-latest}"
REPO="${REPO:-https://github.com/ReduxISU/Redux_Build_System}"

# Install system-wide: features run as root, but every user in the container needs `rbs`.
export UV_INSTALL_DIR="/usr/local/bin"
export UV_TOOL_BIN_DIR="/usr/local/bin"
export UV_TOOL_DIR="/usr/local/share/uv/tools"
export UV_PYTHON_INSTALL_DIR="/usr/local/share/uv/python"
export INSTALLER_NO_MODIFY_PATH=1

if ! command -v curl >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y --no-install-recommends ca-certificates curl git
  rm -rf /var/lib/apt/lists/*
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

if [ "${VERSION}" = "latest" ]; then
  SPEC="git+${REPO}"
else
  SPEC="git+${REPO}@${VERSION}"
fi

# --python 3.12 makes this work on images with no Python (dotnet, node): uv fetches a managed one.
uv tool install --python 3.12 "${SPEC}"

chmod -R a+rX /usr/local/share/uv

rbs --version
