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
