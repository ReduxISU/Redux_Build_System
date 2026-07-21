# Redux Build System (`rbs`)

Centralized CI/CD build-automation for the [ISU Redux](https://github.com/ReduxISU) repos.

The pipeline logic lives here as a Python CLI, **not** in per-repo YAML. Each consumer repo carries
only a small `rbs.toml` and a thin caller workflow; the GitHub Action installs `rbs` and runs one
operation per step. The **same** CLI runs in three places — a local terminal, a devcontainer, and the
PR action — so a contributor can reproduce the whole pipeline (or one step) locally before pushing.

## Model

- **Operations** are the shared vocabulary: `audit`, `format-check`, `lint`, `unit-test`, `build`,
  `integration-test`, `push` (plus `report`, and later `deploy`).
- **Engines** are toolchain modules that implement those operations. `UvEngine` (Python/uv) is first;
  `DotnetEngine` and `NpmEngine` follow. Adding a stack = one new engine class against the shared
  interface in `src/redux_build/engines/base.py`.
- A consumer's `rbs.toml` picks the engine (`engine = "uv"`) and supplies parameters.

## Usage

```bash
uv sync --all-groups
uv run rbs --help
uv run rbs lint          # run one operation against the current repo (reads ./rbs.toml)
uv run rbs ci            # run the engine's full ordered pipeline locally
```

In CI, a consumer repo calls a reusable workflow:

```yaml
jobs:
  ci:
    uses: ReduxISU/Redux_Build_System/.github/workflows/ci-uv.yml@v1
    secrets: inherit
```

Goals: **(A)** build/test, **(B)** aggregate every operation's result into one sticky PR comment,
**(C)** deploy (later). See `docs/` for the engine contract and onboarding.
