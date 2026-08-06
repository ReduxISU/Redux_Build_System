# Onboarding a repo

A consumer repo needs two files (plus, for `integration-test`, an integration suite), and usually a
devcontainer that pulls in the `rbs` feature.

## 0. Devcontainer (recommended)

Add the `rbs` feature next to the docker feature so gates run inside the container:

```jsonc
"features": {
  "ghcr.io/devcontainers/features/docker-outside-of-docker:1": { "moby": false },
  "ghcr.io/reduxisu/features/rbs:1": {}
}
```

Two things that will bite otherwise:

- **`"moby": false` is required** on Debian *trixie* base images (the current `python` and `base`
  devcontainer images). `moby-cli` is not packaged for trixie and the build fails outright.
- **Mount a volume over the dependency directory.** The repo is bind-mounted, so a host-built
  `.venv` / `node_modules` / `obj` contains host absolute paths and the container will rewrite it,
  breaking the host toolchain (and vice versa). Give it a container-local volume and `chown` it in
  `postCreateCommand`:

  ```jsonc
  "mounts": [
    "source=<repo>-venv,target=${containerWorkspaceFolder}/.venv,type=volume"
  ]
  ```
  ```bash
  sudo chown "$(id -u):$(id -g)" .venv    # volumes are created root-owned
  ```

## 1. `rbs.toml` at the repo root

```toml
engine = "uv"                       # uv | dotnet | npm
package = "quantumsolver"

[unit-test]
python-versions = ["3.12", "3.13"]
coverage-min = 85

[artifact]
image = "ghcr.io/reduxisu/quantumsolver"
port = 27100
health-path = "/health"

[integration]
command = "uv run pytest tests/integration -v"

[push]
on-branch = "main"
```

### `[integration]` in full

`integration-test` starts the image `build` just produced on a throwaway network, waits for it to
answer on `[artifact] health-path`, and runs `command`. A repo whose artifact needs company
declares those services too — the GUI's proxy is useless without a backend behind it:

```toml
[integration]
command = "npm run test:e2e"
timeout = 240                       # seconds to wait for readiness; default 180

# Started before the artifact, in the order declared, each reachable at its `name`.
[[integration.services]]
name = "redux-api"
image = "ghcr.io/reduxisu/redux:latest"
port = 27000
health-path = "/Navigation/Batch/allProblems"

# Environment for the artifact container. Address dependencies by service name.
[integration.env]
REDUX_BASE_URL = "http://redux-api:27000/"
```

A service may carry its own `env` the same way (`[integration.services.env]`, or inline
`env = { KEY = "value" }`), which is how you would point Redux at a quantumsolver container.

**`command` receives the addresses in its environment** — do not hardcode them:

| Variable | Value |
|---|---|
| `RBS_BASE_URL` | the artifact under test |
| `RBS_URL_<SERVICE>` | each service, name upper-cased with `-` → `_` (e.g. `RBS_URL_REDUX_API`) |

The suite must not start anything itself. Lifecycle belongs to rbs so that a laptop, a devcontainer
and CI all bring the stack up the same way.

**Health paths need to mean "ready", not "listening".** Pick a route that exercises the app's real
startup. Redux has no `/health`, so it uses `Navigation/Batch/allProblems`, whose first call forces
the assembly scan that builds the problem registry.

If a service never becomes ready, the operation fails with that container's last log lines attached
as findings — and it fails immediately if the container has already exited, rather than waiting out
the timeout.

## 2. A thin caller workflow

`.github/workflows/ci.yml`:

```yaml
on:
  pull_request: { branches: [main] }
  push: { branches: [main] }
permissions:
  contents: read
  packages: write
  pull-requests: write
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  ci:
    uses: ReduxISU/Redux_Build_System/.github/workflows/ci-uv.yml@v1
    secrets: inherit
```

Pick the reusable workflow that matches the engine (`ci-uv.yml`, `ci-dotnet.yml`, `ci-npm.yml`).

## 3. Try it locally first

```bash
uvx --from git+https://github.com/ReduxISU/Redux_Build_System rbs ci
```
