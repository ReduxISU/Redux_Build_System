# Redux Build System (`rbs`)

Centralized CI/CD build automation for the [ISU Redux](https://github.com/ReduxISU) repos —
`Redux` (.NET), `Redux_GUI` (Next.js), `quantumsolver` (Python), and anything added later.

`rbs` is a **Python CLI that is the pipeline**. GitHub Actions does not contain the build logic; it
installs this CLI and calls it. The same binary runs in a terminal, in a devcontainer, and in CI —
so "it passed locally" and "it passed in the PR" mean the same thing.

---

## Why this exists

Three problems in the Redux repos this is built to solve:

1. **Pipeline logic was trapped in YAML.** It could only run on GitHub. You could not reproduce a
   failing gate locally without pushing a commit and waiting.
2. **Every repo drifted.** `quantumsolver` gates on `pip-audit → black → ruff → pytest --cov-fail-under=85`;
   `Redux` runs build+test only; `Redux_GUI` has no tests and a non-blocking linter. There was no
   shared floor and no single place to raise it.
3. **The tested artifact was not the shipped artifact.** CI built and pushed an image, but tests ran
   against the source tree, never the image. `rbs` builds the artifact, tests **that** artifact, and
   only then pushes it.

Adding a gate to all repos should be one change here — not three PRs in three languages.

---

## The model

### Operations — the shared vocabulary

Every repo, in every language, exposes the same verbs:

| Operation | Meaning |
|---|---|
| `audit` | Dependency vulnerability scan |
| `format-check` | Verify formatting, write nothing |
| `lint` | Static analysis |
| `unit-test` | Unit tests + coverage gate |
| `build` | Build the deployable artifact **locally** (never pushes) |
| `integration-test` | Run tests against the locally-built artifact over HTTP |
| `push` | Tag and push the **already-tested** artifact to the registry |
| `report` | Aggregate all results into one report |

`mise run test` / `rbs unit-test` means the same thing whether the repo is C#, Python, or JS.

### Engines — toolchain modules

An **engine** implements those operations for one toolchain. The engine owns the whole set; there is
no `lint` module that branches on language.

| Engine | Toolchain | Repos | Status |
|---|---|---|---|
| `uv` | Python + uv (ruff, black, pytest, pip-audit) | `quantumsolver`, this repo | implemented |
| `dotnet` | .NET SDK (`dotnet test`, analyzers) | `Redux` | planned |
| `npm` | Node (eslint, biome, next build) | `Redux_GUI` | planned |

Adding a stack = one new class in `src/redux_build/engines/` subclassing `Engine`. Container
operations (`build` / `integration-test` / `push`) are identical everywhere, so they live on the base
class and every engine inherits them; a toolchain can still override any of them.

A repo picks its engine in `rbs.toml`:

```toml
engine = "uv"
```

### Fragments — the result contract

Every operation returns a **Fragment**, the unit the reporter aggregates:

```json
{
  "engine": "uv",
  "operation": "unit-test",
  "status": "success",
  "summary": "142 passed · coverage 91%",
  "variant": "py3.12",
  "metrics": {},
  "duration_s": 37.2
}
```

`status` is one of `success` / `failure` / `skipped` / `warning`. Schema:
[`schemas/report-fragment.schema.json`](schemas/report-fragment.schema.json). Operations are pure —
they return a Fragment and the CLI performs the side effects — which is why they are unit-testable.

---

## How a run works

```
rbs <operation>
      │
      ├─ read ./rbs.toml            → which engine, which parameters
      ├─ RunContext.detect()        → local or GitHub? (GITHUB_ACTIONS)
      ├─ engine.<operation>(ctx)    → shells out to uv / docker / …
      └─ report.emit(ctx, fragment)
             ├─ write  $RBS_REPORT_DIR/<operation>[-<variant>].json   (always)
             ├─ print  a one-line result                              (always)
             ├─ append $GITHUB_STEP_SUMMARY                           (CI only)
             └─ append $GITHUB_OUTPUT  status=…                       (CI only)

rbs report → load every fragment → render markdown → (--post) upsert one sticky PR comment
```

Exit code is non-zero when the operation fails, so each step can gate the next.

**The artifact hand-off — the point of the whole design:**

```
build ──→ local/<name>:ci ──→ integration-test (runs THAT image) ──→ push (retags THAT image)
             (never pushed until it has passed)
```

Nothing is uploaded to a registry to be pulled back down. The bytes that were tested are the bytes
that ship.

---

## Install

The CLI needs [`uv`](https://docs.astral.sh/uv/). Docker is needed only for `build` /
`integration-test` / `push`.

```bash
# ephemeral — no install, always current
uvx --from ~/projects/ISU/Redux_Build_System rbs --help

# on PATH, tracks your edits live
uv tool install --editable ~/projects/ISU/Redux_Build_System

# or from inside this repo
uv sync --all-groups && uv run rbs --help
```

---

## Local usage

`rbs` acts on the **current working directory** and reads `./rbs.toml` from it.

```bash
cd ~/projects/ISU/quantumsolver

rbs lint                      # one gate
rbs unit-test --variant py3.12
rbs build                     # → local/quantumsolver:ci
rbs report                    # aggregate everything run so far
```

Results accumulate in `.rbs/report/` until you clear it, so you can run gates one at a time and then
render a combined report. Add `.rbs/` and `report.md` to the consumer repo's `.gitignore`.

Useful flags and variables:

| | |
|---|---|
| `--variant <label>` | Tags the fragment (e.g. `py3.12`) so matrix runs don't overwrite each other |
| `report --post` | Upsert the report as a sticky PR comment (CI only; no-op without a token) |
| `report --soft` | Render the report but exit 0 even if an operation failed |
| `RBS_REPORT_DIR` | Where fragments are written (default `./.rbs/report`) |

---

## Configuration — `rbs.toml`

Lives at the root of the **consumer** repo.

```toml
engine = "uv"                              # required — which toolchain engine
package = "quantumsolver"                  # coverage target; fallback artifact name

[unit-test]
coverage-min = 85                          # --cov-fail-under

[artifact]
image = "ghcr.io/reduxisu/quantumsolver"   # registry name; local tag derives from it
dockerfile = "Dockerfile"                  # optional, default "Dockerfile"
context = "."                              # optional, default "."
```

**Consumed today:** `engine`, `package`, `unit-test.coverage-min`, `artifact.{image,name,dockerfile,context}`.
Keys for unimplemented operations (`[integration]`, `[push]`, matrix `python-versions`) are defined in
[`docs/onboarding.md`](docs/onboarding.md) and are inert until those operations land.

`build` derives `local/<name>:ci` from `artifact.name`, else the last path segment of
`artifact.image`, else `package`.

---

## Testing it in a devcontainer

This repo ships a devcontainer so `rbs` can be exercised in the same kind of sandbox the consumer
repos use. It includes **docker-outside-of-docker**, so `rbs build` inside the container drives the
host Docker daemon and the images it builds are visible from your host.

It works headless from a terminal — no VS Code needed, which is the point if you want it sitting in
its own tmux pane.

```bash
npm install -g @devcontainers/cli          # once (or use `npx @devcontainers/cli` below)

cd ~/projects/ISU/Redux_Build_System
devcontainer up --workspace-folder .       # build + start; postCreate runs uv sync

# run gates inside the container
devcontainer exec --workspace-folder . rbs lint
devcontainer exec --workspace-folder . rbs unit-test
devcontainer exec --workspace-folder . mise run test

# or get a shell and stay there (good for a dedicated pane)
devcontainer exec --workspace-folder . bash
```

To drive **another** repo from that pane, mount it and run `rbs` against it:

```bash
cd ~/projects/ISU/quantumsolver
devcontainer up --workspace-folder .       # once that repo has its own devcontainer
```

Or, without any devcontainer, the same thing in plain Docker:

```bash
docker run --rm -it \
  -v ~/projects/ISU:/work -w /work/quantumsolver \
  -v /var/run/docker.sock:/var/run/docker.sock \
  mcr.microsoft.com/devcontainers/python:3.12 bash
```

**Note:** with docker-outside-of-docker, containers started by `rbs` are siblings on the host, not
children. Bind mounts inside `rbs` resolve against **host** paths, and a service on port 27100 is
reachable at `localhost:27100` on the host.

**Gotcha — `"moby": false` is required.** The `python:3.12` devcontainer base is now Debian
*trixie*, which has no `moby-cli` package, so the docker feature's default (`moby: true`) fails the
build outright. This config pins `moby: false` to use the upstream Docker CE CLI instead. Any other
repo adding a docker feature on a trixie-based image needs the same setting.

---

## How it works in a GitHub Action

> **Status: not yet implemented.** The reusable workflows described here are the next milestone.
> Everything above this section works today.

A consumer repo will carry two files and no pipeline logic:

**`rbs.toml`** (above) and **`.github/workflows/ci.yml`**:

```yaml
name: CI
on:
  pull_request: { branches: [main] }
  push: { branches: [main] }

permissions:
  contents: read          # checkout
  packages: write         # push → GHCR
  pull-requests: write    # sticky report comment

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  ci:
    uses: ReduxISU/Redux_Build_System/.github/workflows/ci-uv.yml@v1
    secrets: inherit
```

The hub's reusable workflow installs `rbs` and calls one operation per step:

```yaml
jobs:
  quality:                     # matrix: 3.12 / 3.13
    steps:
      - uses: ./.github/actions/setup-rbs
      - run: rbs audit
      - run: rbs format-check
      - run: rbs lint
      - run: rbs unit-test --variant py${{ matrix.python }}
      - uses: actions/upload-artifact@v4      # report-quality-<ver>

  build-test:                  # needs: quality
    steps:
      - run: rbs build                        # local image, not pushed
      - run: rbs integration-test             # against that image
      - run: rbs push                         # only if the above passed

  report:                      # needs: [quality, build-test], if: always()
    steps:
      - uses: actions/download-artifact@v4    # pattern: report-*, merge-multiple
      - run: rbs report --post                # one sticky PR comment
```

Why permissions are declared in the **caller**: a reusable workflow can only narrow `GITHUB_TOKEN`
scope, never widen it. Fork PRs get a read-only token and no secrets — push and comment are skipped
by design, and the report still renders in the job summary.

### The report (Goal B)

Each operation drops a JSON fragment; each job uploads its fragments as an artifact; a final job
merges them and upserts **one** comment, keyed by an HTML marker so re-runs edit it instead of
spamming the thread:

```markdown
## Redux Build System — CI Report
`uv` · commit `a1b2c3d`

| Operation | Status | Summary |
|---|:--:|---|
| audit | ✅ | no known vulnerabilities |
| format-check | ✅ | all files formatted |
| lint | ✅ | 0 issues |
| unit-test · py3.12 | ✅ | 142 passed · coverage 91% |
| unit-test · py3.13 | ✅ | 142 passed · coverage 91% |
| build | ✅ | built `local/quantumsolver:ci` · 610MB |
| integration-test | ✅ | /health 200 · 6/6 checks |
| push | ⏭️ | only on push to `main` |

**Overall: ✅ 7 passed · 0 failed · 1 skipped**
```

---

## Status

| Capability | State |
|---|---|
| `audit`, `format-check`, `lint`, `unit-test` (uv engine) | ✅ implemented |
| `build` — local image via `docker buildx --load` | ✅ implemented |
| `report` — fragments, markdown, sticky-comment poster | ✅ implemented |
| `integration-test` | ⬜ next |
| `push` | ⬜ planned |
| `rbs ci` — run the engine's full ordered pipeline | ⬜ planned |
| Reusable workflows + `setup-rbs` action | ⬜ planned |
| `dotnet` / `npm` engines | ⬜ planned |
| `deploy` — pull-based compose deploy | ⬜ planned |

Unimplemented operations report `skipped`, never a false pass.

---

## Development

This repo runs its own pipeline against itself:

```bash
uv sync --all-groups
uv run pytest            # the build system's own tests
uv run rbs lint          # dogfooding — rbs linting rbs
mise run test            # same thing through the shared task vocabulary
```

Layout:

```
src/redux_build/
├─ cli.py          Typer app — one subcommand per operation
├─ config.py       rbs.toml loading
├─ context.py      RunContext — cwd, local-vs-CI detection, paths
├─ runner.py       shell-out helper (rc, output, duration)
├─ models.py       Fragment + Status
├─ report.py       fragments → markdown → sticky PR comment
├─ docker.py       image tag derivation, size
├─ registry.py     engine name → class
└─ engines/
   ├─ base.py      Engine ABC + shared container operations
   └─ uv.py        UvEngine
```

See [`docs/engine-contract.md`](docs/engine-contract.md) to add an engine and
[`docs/onboarding.md`](docs/onboarding.md) to onboard a repo.
