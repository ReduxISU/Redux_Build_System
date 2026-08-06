#!/usr/bin/env bash
# One command from nothing to a shell inside a consumer repo's dev container, with `rbs` on the
# PATH and the docker socket wired up — the loop for running the full pipeline while developing.
# Defaults to Redux_GUI.
#
# `devcontainer up` on its own mostly works; what this adds is the preflight, because the three
# ways it fails all look like something else:
#   * port already taken  -> "Bind for 0.0.0.0:3000 failed"
#   * a half-created container from a previous failure gets REUSED, with no network routes at
#     all, which surfaces as a DNS error in postCreate and looks like your network is broken
#   * an interrupted `rbs integration-test` leaves containers and a network behind, and the dev
#     container still attached to that network
set -euo pipefail

HUB="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../rbs_dev.sh
source "${HUB}/rbs_dev.sh" # devc(): the devcontainer CLI, falling back to npx

TARGET="${HUB}/../Redux_GUI"
APP_PORT=3000
DEV_RBS=0
FORCE=0
REBUILD=0

usage() {
  cat <<'EOF'
Usage:
  scripts/dev-gui.sh [options] [command]

Commands:
  shell              open a shell in the dev container (default)
  ci                 run `rbs ci` in it
  run <args...>      run `rbs <args...>` in it
  down               remove the dev container (named volumes kept)
  reset              down, and drop the repo's node_modules/.next volumes

Options:
  -t, --target DIR   repo to work on            (default: ../Redux_GUI beside this one)
      --dev-rbs      use THIS working tree's rbs instead of the published feature
      --force        reclaim the app port if another container is holding it
      --rebuild      rebuild the image from scratch (picks up a newer rbs feature)
  -h, --help         this

Without --dev-rbs you get the published rbs feature — what a student contributor gets, and so
the path worth checking before asking anyone to use it.
EOF
}

say() { echo "dev-gui: $*"; }

container_id() {
  docker ps -aq --filter "label=devcontainer.local_folder=${TARGET}" | head -1
}

running_id() {
  docker ps -q --filter "label=devcontainer.local_folder=${TARGET}" | head -1
}

require_docker() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  say "cannot reach the docker daemon." >&2
  say "if that was a permission error, you are not in the docker group:" >&2
  echo "    sudo usermod -aG docker \$USER && newgrp docker" >&2
  exit 1
}

# An interrupted `rbs integration-test` leaves labelled containers, a network, and quite possibly
# this dev container still attached to it. Clearing that first is why `down` is also the fix when
# a run is killed halfway.
sweep_orphans() {
  local ids nets net attached
  ids="$(docker ps -aq --filter "label=rbs.run")"
  if [ -n "${ids}" ]; then
    say "removing leftover integration containers"
    # shellcheck disable=SC2086
    docker rm -f ${ids} >/dev/null
  fi
  nets="$(docker network ls -q --filter "label=rbs.run")"
  for net in ${nets}; do
    say "removing leftover integration network ${net}"
    attached="$(docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' "${net}" 2>/dev/null || true)"
    for container in ${attached}; do
      docker network disconnect -f "${net}" "${container}" >/dev/null 2>&1 || true
    done
    docker network rm "${net}" >/dev/null 2>&1 || true
  done
}

free_port() {
  local own holders remaining names
  own="$(running_id)"
  holders="$(docker ps -q --filter "publish=${APP_PORT}")"
  remaining=""
  for holder in ${holders}; do
    if [ "${holder}" != "${own}" ]; then
      remaining="${remaining} ${holder}"
    fi
  done
  if [ -z "${remaining# }" ]; then
    return 0
  fi
  # shellcheck disable=SC2086
  names="$(docker inspect -f '{{.Name}}' ${remaining} 2>/dev/null | tr -d '/' | xargs || true)"
  if [ "${FORCE}" -eq 1 ]; then
    say "reclaiming port ${APP_PORT} from:${names:+ }${names}"
    # shellcheck disable=SC2086
    docker rm -f ${remaining} >/dev/null
    return 0
  fi
  say "port ${APP_PORT} is held by:${names:+ }${names}" >&2
  say "the dev container publishes it, so startup would fail. Re-run with --force, or:" >&2
  echo "    docker rm -f ${names}" >&2
  exit 1
}

# A `devcontainer up` that failed during network setup leaves the container behind, and the next
# `up` reuses that same id -- with no routes. Every retry then fails at postCreate with a DNS
# error that has nothing to do with DNS. Removing it first is the whole fix.
sweep_stale() {
  local id state
  id="$(container_id)"
  if [ -z "${id}" ]; then
    return 0
  fi
  state="$(docker inspect -f '{{.State.Status}}' "${id}" 2>/dev/null || echo unknown)"
  if [ "${state}" = "running" ]; then
    return 0
  fi
  say "removing a ${state} dev container so it gets rebuilt with working networking"
  docker rm -f "${id}" >/dev/null
}

expected_config() {
  if [ "${DEV_RBS}" -eq 1 ]; then
    echo "${TARGET}/.devcontainer/.rbs-dev/devcontainer.json"
  else
    echo "${TARGET}/.devcontainer/devcontainer.json"
  fi
}

# --dev-rbs builds a second container from a rewritten config, and both publish the app port, so
# only one mode can be up at a time. Switching modes silently collides on the port otherwise.
drop_other_mode() {
  local want config
  want="$(expected_config)"
  for id in $(docker ps -aq --filter "label=devcontainer.local_folder=${TARGET}"); do
    config="$(docker inspect -f '{{index .Config.Labels "devcontainer.config_file"}}' "${id}" 2>/dev/null || true)"
    if [ -n "${config}" ] && [ "${config}" != "${want}" ]; then
      say "removing the dev container from the other mode (it holds port ${APP_PORT})"
      docker rm -f "${id}" >/dev/null
    fi
  done
}

preflight() {
  require_docker
  if [ ! -f "${TARGET}/.devcontainer/devcontainer.json" ]; then
    say "no .devcontainer/devcontainer.json in ${TARGET}" >&2
    exit 1
  fi
  sweep_orphans
  drop_other_mode
  free_port
  sweep_stale
}

hint() {
  cat <<EOF

  Inside the container:
    rbs ci                 the whole pipeline, exactly as the PR runs it
    rbs build              just the image
    rbs integration-test   the image plus the services it needs

EOF
}

# The rbs feature is installed into the devcontainer IMAGE, so a cached image pins rbs to whenever
# that image was built -- anything pushed to the hub since is simply not in there. It surfaces as a
# bare "No such command", which points at nothing. `ci` is the canary because it is the newest
# top-level command; update it if a newer one becomes the obvious probe.
warn_if_stale() {
  if devc exec --workspace-folder "${TARGET}" rbs ci --help >/dev/null 2>&1; then
    return 0
  fi
  say "this container's rbs is older than the hub — its image came from cache." >&2
  say "re-run with --rebuild to reinstall it, or --dev-rbs to use this working tree." >&2
}

launch() {
  preflight
  if [ "${1}" = "shell" ]; then
    hint
  fi
  if [ "${DEV_RBS}" -eq 1 ]; then
    exec "${HUB}/rbs_dev.sh" -c -t "${TARGET}" "$@"
  fi
  if [ "${REBUILD}" -eq 1 ]; then
    devc up --workspace-folder "${TARGET}" --build-no-cache --remove-existing-container
  else
    devc up --workspace-folder "${TARGET}"
    warn_if_stale
  fi
  if [ "${1}" = "shell" ]; then
    devc exec --workspace-folder "${TARGET}" bash
  else
    devc exec --workspace-folder "${TARGET}" rbs "$@"
  fi
}

down() {
  require_docker
  sweep_orphans
  local id
  id="$(container_id)"
  if [ -n "${id}" ]; then
    docker rm -f "${id}" >/dev/null
    say "removed the dev container (named volumes kept)"
  fi
  # Also clears the scratch config rbs_dev.sh writes under --dev-rbs.
  "${HUB}/rbs_dev.sh" --down -t "${TARGET}" >/dev/null 2>&1 || true
}

# Dependency volumes are container-local on purpose (a host-built node_modules holds host paths),
# so a broken install can only be cleared by dropping them.
reset() {
  down
  local volumes
  volumes="$(grep -o 'source=[^,]*,type=volume' "${TARGET}/.devcontainer/devcontainer.json" |
    sed 's/^source=//; s/,.*//')"
  for volume in ${volumes}; do
    if docker volume rm "${volume}" >/dev/null 2>&1; then
      say "dropped volume ${volume}"
    fi
  done
}

while [ $# -gt 0 ]; do
  case "$1" in
  -t | --target)
    TARGET="$(cd "$2" && pwd)"
    shift 2
    ;;
  --dev-rbs)
    DEV_RBS=1
    shift
    ;;
  --force)
    FORCE=1
    shift
    ;;
  --rebuild)
    REBUILD=1
    shift
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *) break ;;
  esac
done

TARGET="$(cd "${TARGET}" 2>/dev/null && pwd || echo "${TARGET}")"

case "${1:-shell}" in
shell) launch shell ;;
down) down ;;
reset) reset ;;
run)
  shift
  [ $# -gt 0 ] || {
    usage
    exit 1
  }
  launch "$@"
  ;;
*) launch "$@" ;;
esac
