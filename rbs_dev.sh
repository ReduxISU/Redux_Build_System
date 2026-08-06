#!/usr/bin/env bash
# Run a development build of rbs (this working tree) against another repo.
# Maintainer tool: consumers use the published feature instead.
set -euo pipefail

RBS_HOME="${RBS_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
TARGET="${RBS_TARGET:-$PWD}"
TMPDIR_REL=".devcontainer/.rbs-dev"
SRC_MOUNT="/opt/rbs-src"

usage() {
  cat <<'EOF'
Usage:
  rbs_dev.sh <rbs args...>              run dev rbs on the host, against $PWD
  rbs_dev.sh -c|--container <rbs args>  run dev rbs inside the target's devcontainer
  rbs_dev.sh -c shell                   open a shell in that devcontainer
  rbs_dev.sh --down                     remove the dev container and temp config
  rbs_dev.sh -t|--target DIR ...        operate on DIR instead of $PWD

Container mode rebuilds the target's devcontainer with this working tree bind-mounted
and installed editable, so source edits take effect without a rebuild.
EOF
}

devc() {
  if command -v devcontainer >/dev/null 2>&1; then devcontainer "$@"; else npx --yes @devcontainers/cli "$@"; fi
}

host_run() {
  cd "$TARGET"
  if [ -x "$RBS_HOME/.venv/bin/rbs" ]; then
    exec "$RBS_HOME/.venv/bin/rbs" "$@"
  fi
  exec uvx --from "$RBS_HOME" rbs "$@"
}

write_config() {
  local dir="$TARGET/$TMPDIR_REL"
  rm -rf "$dir"
  mkdir -p "$dir"
  cp -r "$RBS_HOME/features/src/rbs" "$dir/rbs"
  RBS_HOME="$RBS_HOME" SRC_MOUNT="$SRC_MOUNT" python3 - "$TARGET/.devcontainer/devcontainer.json" "$dir/devcontainer.json" <<'PY'
import json, os, re, sys

raw = open(sys.argv[1]).read()
try:
    cfg = json.loads(raw)
except json.JSONDecodeError:
    cfg = json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))

# Point the rbs feature at the local copy; leave every other feature alone.
def is_rbs(key):
    return key.split("@")[0].rsplit(":", 1)[0].rstrip("/").rsplit("/", 1)[-1] == "rbs"

features = cfg.get("features", {})
cfg["features"] = {("./rbs" if is_rbs(k) else k): v for k, v in features.items()}

mounts = list(cfg.get("mounts", []))
mounts.append(f"source={os.environ['RBS_HOME']},target={os.environ['SRC_MOUNT']},type=bind")
cfg["mounts"] = mounts

# Reinstall from the mounted working tree so the container runs THIS source, not git main.
editable = (
    "sudo env UV_TOOL_BIN_DIR=/usr/local/bin UV_TOOL_DIR=/usr/local/share/uv/tools "
    "UV_PYTHON_INSTALL_DIR=/usr/local/share/uv/python "
    f"uv tool install --force --editable {os.environ['SRC_MOUNT']}"
)
existing = cfg.get("postCreateCommand")
if isinstance(existing, str):
    cfg["postCreateCommand"] = f"{existing} && {editable}"
elif existing is None:
    cfg["postCreateCommand"] = editable
else:
    sys.exit("rbs_dev: postCreateCommand must be a string; got %s" % type(existing).__name__)

json.dump(cfg, open(sys.argv[2], "w"), indent=2)
PY
  # Keep the scratch dir out of the target's git status without touching tracked .gitignore.
  local exclude="$TARGET/.git/info/exclude"
  if [ -f "$exclude" ] && ! grep -qxF "$TMPDIR_REL/" "$exclude"; then
    echo "$TMPDIR_REL/" >> "$exclude"
  fi
}

container_run() {
  [ -f "$TARGET/.devcontainer/devcontainer.json" ] || { echo "rbs_dev: $TARGET has no .devcontainer/devcontainer.json" >&2; exit 1; }
  write_config
  local cfg="$TMPDIR_REL/devcontainer.json"
  devc up --workspace-folder "$TARGET" --config "$TARGET/$cfg" >/dev/null
  if [ "${1:-shell}" = "shell" ]; then
    devc exec --workspace-folder "$TARGET" --config "$TARGET/$cfg" bash
  else
    devc exec --workspace-folder "$TARGET" --config "$TARGET/$cfg" rbs "$@"
  fi
}

down() {
  local cfg="$TARGET/$TMPDIR_REL/devcontainer.json"
  if [ -f "$cfg" ]; then
    local id
    id="$(docker ps -aq --filter "label=devcontainer.config_file=$cfg" 2>/dev/null || true)"
    [ -n "$id" ] && docker rm -f $id >/dev/null
  fi
  rm -rf "$TARGET/$TMPDIR_REL"
  echo "rbs_dev: removed dev container and $TMPDIR_REL (named volumes kept)"
}

# Being sourced (scripts/dev-gui.sh does, to reuse devc) must define the helpers and stop there.
if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
  return 0
fi

MODE=host
while [ $# -gt 0 ]; do
  case "$1" in
    -c|--container) MODE=container; shift ;;
    -t|--target) TARGET="$(cd "$2" && pwd)"; shift 2 ;;
    --down) MODE=down; shift ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;
  esac
done

case "$MODE" in
  host) [ $# -gt 0 ] || { usage; exit 1; }; host_run "$@" ;;
  container) container_run "$@" ;;
  down) down ;;
esac
