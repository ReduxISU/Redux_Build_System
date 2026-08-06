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
| `npm` | Node (npm audit, biome, eslint) | `Redux_GUI` | implemented |
| `dotnet` | .NET SDK (`dotnet list package`/`format`/`build`/`test`) | `Redux` | implemented |

Two things the `npm` engine does deliberately:

- **`audit` runs `npm audit --omit=dev`**, so it scans only what ships — the same surface the `uv`
  engine gets from `uv export --no-dev`. Including devDependencies would gate a release on
  vulnerabilities in the linter.
- **`unit-test` reports `skipped` when `package.json` has no `test` script**, rather than passing
  vacuously. It activates on its own the moment a suite is added.

Tools run via `npx --no-install` — the node analogue of `uv run` — so a missing devDependency fails
loudly instead of being silently fetched from the registry mid-gate.

The `dotnet` engine needs no third-party tooling — the SDK is the whole toolchain — but two things
about it are worth knowing:

- **`lint` is `dotnet build`.** `Redux/Directory.Build.props` sets `TreatWarningsAsErrors`, so the
  compiler and analyzers together are the lint gate. `format-check` and `lint` emit the same MSBuild
  diagnostic format, so one parser turns both into findings (deduped — MSBuild repeats a diagnostic
  once per referencing project).
- **`audit` ignores the exit code.** `dotnet list package --vulnerable` exits 0 whether or not it
  finds anything, so status comes from the JSON document instead. Getting this wrong would be a
  silent false pass.
- **`unit-test` gates on coverage** via `coverlet.collector`, reading the cobertura reports it
  writes to a temp directory. Reports from several test projects are *summed*, not averaged, so a
  small fully-covered project cannot mask a large bare one. A project without the collector simply
  gets no coverage gate rather than a spurious 0%.

The solution is always named explicitly (`solution` in `rbs.toml`, else the single `*.slnx`/`*.sln`
found on disk) because a bare `dotnet build` errors MSB1011 when a `.csproj` sits beside it.

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
  "engine": "npm",
  "operation": "lint",
  "status": "failure",
  "summary": "26 errors, 43 warnings",
  "variant": "",
  "metrics": {},
  "duration_s": 4.4,
  "findings": [
    {
      "severity": "error",
      "location": "components/hooks/ProblemProvider/Problem.js:51",
      "rule": "no-undef",
      "message": "'requestInfo' is not defined."
    }
  ]
}
```

`findings` is what makes the report *actionable* rather than merely informative — a reviewer sees
which file and which rule, not just a count. The shape is deliberately tool-agnostic, so `location`
is `file:line` for a linter, `pkg@range` for an audit, and a test id for a test run. Engines get it
by asking their tools for JSON (`eslint -f json`, `biome --reporter=json`, `npm audit --json`) and
translating; a document that fails to parse yields no findings rather than crashing the run, and the
exit code still gates.

`status` is one of `success` / `failure` / `skipped` / `warning` / `blocked`. Schema:
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

Exit code is non-zero when the operation fails.

### Failure policy — report everything, block only what genuinely can't run

A PR gate's job is to tell you *everything* that is wrong in one pass, not just the first thing.

**Quality gates** (`audit`, `format-check`, `lint`, `unit-test`) have **no prerequisites**. A lint
failure never stops the audit from running. Every one of them reports, every run.

**The artifact chain** is a real data dependency, so it is enforced: you cannot integration-test an
image that failed to build, or push one that failed its tests. Those operations declare
prerequisites (`Engine.requires`), and when one is unmet the operation reports **`blocked`** —
"a required upstream operation failed, so this never ran" — instead of running and failing for the
wrong reason, or reporting a misleading `skipped`.

```
audit ─┐
lint  ─┼─ all always run, independently
…     ─┘
build ──✅/❌──▶ integration-test ──✅/❌──▶ push       ❌ upstream ⇒ ⛔ blocked downstream
```

Crucially **that policy lives here, not in workflow YAML.** `rbs integration-test` reads the
fragments already on disk and short-circuits itself, so it behaves identically on your laptop and in
CI. Workflow steps therefore carry a uniform, policy-free `continue-on-error: true`, and
**`rbs report` is the single gate** that fails the build:

```yaml
- run: rbs audit
  continue-on-error: true      # identical on every operation step — no policy in YAML
- run: rbs lint
  continue-on-error: true
…
- run: rbs report --post       # the only step that can fail the job
```

For branch protection / rulesets this means you require **one** status check — the report job —
rather than one per operation. It cannot go stale as operations are added.

### Test matrices run inside one container

`[unit-test] python-versions` fans `unit-test` out over several interpreters, and `rbs ci` runs
each leg in sequence:

```
| unit-test · 3.12 | ✅ | 65 passed · coverage 86% | 27.5s |
| unit-test · 3.13 | ✅ | 65 passed · coverage 86% | 16.3s |
```

This is deliberately **not** a workflow matrix. `uv` fetches interpreters on demand, so both legs
run inside the single devcontainer the job already built — a GitHub matrix would rebuild that image
once per leg and roughly double the wall clock for no extra coverage. Everything else still runs
once; only operations an engine declares in `variants()` fan out.

`rbs unit-test --variant 3.12` (or `py3.12`) pins a single leg, and an explicit `--variant` on
`rbs ci` does the same for the whole run.

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

**Consumed today:** `engine`, `package`, `unit-test.{coverage-min,python-versions}`,
`artifact.{image,name,dockerfile,context,port,health-path}`,
`integration.{command,timeout,env,services}`.
Keys for `[push]` are defined in [`docs/onboarding.md`](docs/onboarding.md) and are inert until that
operation lands. The full `[integration]` schema — dependency services, their environment, and the
`RBS_BASE_URL` / `RBS_URL_<SERVICE>` variables handed to the suite — is documented there too.

`build` derives `local/<name>:ci` from `artifact.name`, else the last path segment of
`artifact.image`, else `package`.

---

## Getting `rbs` into a repo's devcontainer

`rbs` ships as a **devcontainer Feature**, so a consumer repo does not vendor any install logic — it
lists the feature and gets the CLI on `PATH`:

```jsonc
// quantumsolver/.devcontainer/devcontainer.json
{
  "image": "mcr.microsoft.com/devcontainers/python:3.12",
  "features": {
    "ghcr.io/reduxisu/features/rbs:1": {},
    "ghcr.io/devcontainers/features/docker-outside-of-docker:1": { "moby": false }
  }
}
```

Open that container and you have quantumsolver's toolchain **plus** `rbs lint`, `rbs unit-test`,
`rbs build`. Same feature line works on the .NET base image for `Redux` and the Node one for
`Redux_GUI` — the feature installs a managed Python for itself, so the host image needs no Python.

| Option | Default | Meaning |
|---|---|---|
| `version` | `latest` | Git ref of this repo to install; pin it (e.g. `"version": "v1"`) for reproducibility |
| `repo` | this repo's URL | Source repository, for forks |

**Pair it with `docker-outside-of-docker`.** The feature deliberately does *not* install Docker;
`rbs build` and `rbs integration-test` need a Docker socket, so the consumer adds that feature
alongside it. The `rbs` feature installs *after* `common-utils`, `python`, and the docker feature.

Source lives in [`features/src/rbs/`](features/src/rbs) and publishes to
`ghcr.io/reduxisu/features/rbs` via `.github/workflows/publish-features.yml` on any change to it.
The package is public, so no `docker login` is needed.

**Bump `version` in `devcontainer-feature.json` whenever you change the feature** — republishing an
existing version is a no-op. That version is the feature's own contract and is independent of the
`rbs` CLI version. Test changes before publishing with:

```bash
devcontainer features test --project-folder features --features rbs \
  --base-image mcr.microsoft.com/devcontainers/base:ubuntu
```

---

## Developing `rbs` itself in a devcontainer

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
      - { run: rbs audit,        continue-on-error: true }
      - { run: rbs format-check, continue-on-error: true }
      - { run: rbs lint,         continue-on-error: true }
      - { run: "rbs unit-test --variant py${{ matrix.python }}", continue-on-error: true }
      - uses: actions/upload-artifact@v4      # report-quality-<ver>

  build-test:                  # needs: quality
    steps:                     # rbs itself blocks the chain — see "Failure policy"
      - { run: rbs build,            continue-on-error: true }   # local image, not pushed
      - { run: rbs integration-test, continue-on-error: true }   # ⛔ if build failed
      - { run: rbs push,             continue-on-error: true }   # ⛔ unless both passed
      - uses: actions/upload-artifact@v4

  report:                      # needs: [quality, build-test], if: always()
    steps:
      - uses: actions/download-artifact@v4    # pattern: report-*, merge-multiple
      - run: rbs report --post                # sticky PR comment + job summary; THE gate
```

`if: always()` on the report job is required — without it a failed upstream job would skip the
report and you would lose the very summary explaining what broke.

Why permissions are declared in the **caller**: a reusable workflow can only narrow `GITHUB_TOKEN`
scope, never widen it. Fork PRs get a read-only token and no secrets — push and comment are skipped
by design, and the report still renders in the job summary.

### The report (Goal B)

Each operation drops a JSON fragment; each job uploads its fragments as an artifact; a final job
merges them and renders the table **twice** — into `$GITHUB_STEP_SUMMARY` (the bottom of the Actions
run) and as **one** sticky PR comment, keyed by an HTML marker so re-runs edit it instead of spamming
the thread.

Rows appear in the engine's pipeline order, not alphabetically, so the table reads in the order the
work actually happened:

```markdown
## Redux Build System — CI Report
`uv` · commit `a1b2c3d`

| Operation | Status | Summary | Time |
|---|:--:|---|--:|
| audit | ✅ | no known vulnerabilities | 3.1s |
| format-check | ✅ | all files formatted | 0.9s |
| lint | ✅ | 0 issues | 2.4s |
| unit-test · py3.12 | ✅ | 142 passed · coverage 91% | 37.2s |
| unit-test · py3.13 | ✅ | 142 passed · coverage 91% | 36.8s |
| build | ✅ | built `local/quantumsolver:ci` · 610MB | 48.0s |
| integration-test | ✅ | /health 200 · 6/6 checks | 12.7s |
| push | ⏭️ | only on push to `main` | — |

**Overall: ✅ 7 passed · 0 failed · 1 skipped**
```

A run where the artifact chain breaks reports the failure once and marks the rest `blocked`, so it is
obvious what was never attempted versus what was legitimately not applicable:

```markdown
| build | ❌ | docker build failed | 0.9s |
| integration-test | ⛔ | not run — `build` failed | — |
| push | ⛔ | not run — `build` failed | — |

**Overall: ❌ 0 passed · 1 failed · 0 skipped · 2 blocked**
```

Below the table, any operation with findings gets a collapsed block naming what to actually fix:

```markdown
<details><summary>❌ lint — 26 errors, 43 warnings</summary>

| Severity | Location | Rule | Message |
|---|---|---|---|
| error | components/hooks/ProblemProvider/Problem.js:51 | no-undef | 'requestInfo' is not defined. |
| error | components/pageblocks/VerifyRowReact.js:43 | react-hooks/set-state-in-effect | Calling setState synchronously within an effect… |
| | | | _… and 49 more_ |

</details>
```

At most `MAX_FINDINGS` (20) are listed per operation, most severe first, and the remainder is always
stated — a truncated report must never read as a complete one. Messages are trimmed to their first
line (`react-hooks` rules embed several paragraphs and a code frame); the full text stays in the
fragment JSON.

---

## Status

| Capability | State |
|---|---|
| `audit`, `format-check`, `lint`, `unit-test` (uv engine) | ✅ implemented |
| `audit`, `format-check`, `lint`, `unit-test` (npm engine) | ✅ implemented |
| `audit`, `format-check`, `lint`, `unit-test` (dotnet engine) | ✅ implemented |
| `build` — local image via `docker buildx --load` | ✅ implemented |
| `report` — fragments, markdown, findings, sticky comment + job summary | ✅ implemented |
| `integration-test` — ephemeral stack around the built image | ✅ implemented |
| `rbs ci` — run the engine's full ordered pipeline | ✅ implemented |
| Reusable workflow (`ci.yml`, runs in the caller's devcontainer) | ✅ implemented |
| `push` | ⬜ next |
| `deploy` — pull-based compose deploy | ⬜ planned |

Unimplemented operations report `skipped`, never a false pass.

---

## Development

### `scripts/dev-gui.sh` — the dev loop for a consumer repo

Gets you from nothing to a shell inside a consumer repo's devcontainer with `rbs` on the PATH.
Defaults to `Redux_GUI`; `-t DIR` points it elsewhere.

```bash
scripts/dev-gui.sh                 # shell in the dev container
scripts/dev-gui.sh ci              # run the whole pipeline in it
scripts/dev-gui.sh run build       # run one operation
scripts/dev-gui.sh down            # remove the container (volumes kept)
scripts/dev-gui.sh reset           # ...and drop node_modules/.next volumes
```

`devcontainer up` alone mostly works; the value here is the preflight, because each of these fails
as something that looks unrelated:

- **the app port is already taken** — by a stray dev server, or the *other* mode's container.
  `--force` reclaims it; switching between normal and `--dev-rbs` drops the other automatically,
  since both publish port 3000 and only one can be up.
- **a half-created container gets reused** — a `devcontainer up` that failed during network setup
  leaves a container with no routes, and the next `up` reuses that same id. It then fails in
  `postCreate` with a DNS error that has nothing to do with DNS.
- **an interrupted `rbs integration-test`** leaves labelled containers, a network, and the dev
  container still attached to it. Swept on every launch, and by `down`.
- **a cached image pins rbs** — the feature installs into the *image*, so a hub change pushed since
  that image was built simply is not there, and shows up as a bare `No such command`. The script
  says so and points at `--rebuild` (or `--dev-rbs`, which always installs the working tree).

`--dev-rbs` delegates to `rbs_dev.sh` below.

### `rbs_dev.sh` — testing an unreleased rbs against a real repo

Maintainer tool. Runs **this working tree** against another repo, so you can iterate on an engine
and see the effect immediately. Consumers never need it — they use the published feature.

```bash
cd ~/projects/ISU/quantumsolver

~/projects/ISU/Redux_Build_System/rbs_dev.sh lint            # host: dev rbs against this repo
~/projects/ISU/Redux_Build_System/rbs_dev.sh -c unit-test    # same, inside the repo's devcontainer
~/projects/ISU/Redux_Build_System/rbs_dev.sh -c shell        # shell in that container
~/projects/ISU/Redux_Build_System/rbs_dev.sh --down          # tear it down
```

In container mode it rewrites the target's devcontainer into a scratch
`.devcontainer/.rbs-dev/` config that (a) resolves the `rbs` feature from this repo's local
`features/src/rbs` instead of the registry, and (b) bind-mounts this working tree to `/opt/rbs-src`
and installs it **editable**. Point (b) matters: the feature normally installs rbs from *git*, so
without the override the container would run committed `main`, not your edits. Because the install
is editable, saving a `.py` file changes behavior in the running container with no rebuild.

The scratch dir is added to the target's `.git/info/exclude`, so it never dirties `git status` or a
tracked `.gitignore`.

### The hub's own pipeline

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
