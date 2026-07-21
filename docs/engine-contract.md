# Engine contract

An engine is a toolchain module (`src/redux_build/engines/<name>.py`) subclassing `Engine`
(`base.py`). It sets `name` and `order`, and overrides the operations it supports.

## Operations

Each operation is a method `def <operation>(self, ctx: RunContext) -> Fragment`. Operation names use
underscores as methods (`format_check`) and hyphens on the CLI (`format-check`). An operation an
engine does not support is left to the base default, which returns `status = skipped`.

`order` is the toolchain's default `ci` sequence — the operations `rbs ci` runs, in order.

## Result: `Fragment`

Every operation returns a `Fragment` (`models.py`), the unit the reporter aggregates:

| field | meaning |
|---|---|
| `engine` | e.g. `uv` |
| `operation` | e.g. `lint` |
| `status` | `success` / `failure` / `skipped` / `warning` |
| `summary` | one-line human summary |
| `variant` | matrix label, e.g. `py3.12` |
| `metrics` | optional machine-readable numbers |
| `duration_s` | wall time |

The JSON form is defined in `schemas/report-fragment.schema.json`. Engines stay pure — they return a
`Fragment`; the CLI/reporter handle side effects (console output, step summary, fragment files, the
PR comment), so operations are directly unit-testable.

## Running commands

Use `runner.run(cmd, cwd, env)` to shell out; it captures rc/output and wall time in a `CmdResult`.
Build the `Fragment` from that result.
