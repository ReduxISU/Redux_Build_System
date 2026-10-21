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
command = "uv run pytest integration -v"

[push]
on-branch = "main"                  # the only branch `push` publishes from, and only in CI
```

### `[build]`

`pre-command` is a shell command run in the repo root right before `docker build`, for inputs that
only exist at build time — stamping counts fetched from the GitHub API into a file the Dockerfile
copies, say. The reusable workflow runs the build, so this is the one place a caller can add a step
ahead of it.

```toml
[build]
pre-command = "dotnet run --configuration Release --project Tools/ContributorStatsSync -- --counts-only"
pre-command-local = false           # default; true also runs it on a laptop / local devcontainer
pre-command-optional = false        # default; true builds anyway when it fails, and marks the row ⚠️
```

- **It runs only in CI by default.** Such a hook typically rewrites tracked files, and doing that on
  a developer's machine leaves a dirty tree one `git add` away from being committed. Outside CI the
  build goes ahead without it and the summary says `pre-command skipped outside CI`; set
  `pre-command-local = true` to run it there too.
- **It sees the same environment as the rest of the run**, so `GITHUB_TOKEN` (and the other
  variables the workflow forwards) is available to it.
- **A non-zero exit fails `build`** with `pre-command failed` and no image is built — so
  `integration-test` and `push` are blocked too.
- **For a best-effort step, set `pre-command-optional = true`.** A failed hook then doesn't stop
  `docker build`: the image is built without it, and the `build` row shows ⚠️
  `pre-command failed, built without it`. A warning still counts as passing, so
  `integration-test` and `push` go ahead. Prefer this to making the command non-fatal itself
  (`cmd || true`): that hides the failure, and the row shows ✅ `ran pre-command`.

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

### `[push]`

`push` retags the image `integration-test` just passed as `<image>:<sha7>` and `<image>:latest`
and pushes both to the registry in `[artifact] image`, logging in with `GITHUB_TOKEN`. It runs
**only under GitHub Actions and only when the branch equals `on-branch`**; on a pull request, on a
laptop, or without a `[push]` block it reports `skipped` with the reason, never a failure. It
also reports `blocked` unless **every** earlier operation passed — quality gates included, so a
red `lint` or a coverage shortfall stops the publish even when the image itself is fine. The
caller workflow needs `packages: write`, and the package must already be linked to the repo (or be
created by this repo's workflow) for `GITHUB_TOKEN` to be allowed to push.

## 2. A thin caller workflow

`.github/workflows/rbs.yml`:

```yaml
name: rbs
on:
  pull_request:
  push: { branches: [main] }        # match [push] on-branch
permissions:
  contents: read
  packages: write
  pull-requests: write
concurrency:
  group: rbs-${{ github.ref }}
  cancel-in-progress: true
jobs:
  build-test:
    uses: ReduxISU/Redux_Build_System/.github/workflows/ci.yml@main
    secrets: inherit
```

One workflow serves every engine: it runs `rbs ci` inside the repo's own devcontainer, so the
devcontainer (step 0) is what declares the toolchain. Pass `with: { soft: true }` to report without
failing while rbs runs alongside legacy CI.

Pass `with: { dry-run: true }` to rehearse `push` before trusting it: the CI and branch gates are
bypassed, so a pull request or feature branch logs in to the registry and tags the image exactly as
a release would, then stops short of `docker push`. The report shows
`⚠️ push — dry run — logged in, would push …`. Drop the flag once you have seen that row.

## 3. Try it locally first

```bash
uvx --from git+https://github.com/ReduxISU/Redux_Build_System rbs ci
```
