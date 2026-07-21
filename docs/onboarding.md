# Onboarding a repo

A consumer repo needs two files (plus, for `integration-test`, an integration suite).

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
