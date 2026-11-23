# Contributing to Iki

> **We aren't accepting pull requests from outside contributors yet.** Iki is
> in early development and its architecture is still settling. PRs from
> anyone without write access to the repo are closed automatically.
>
> You can still help: [open an issue](../../issues/new)
> to report a bug or suggest a change. To report a vulnerability, follow
> [SECURITY.md](SECURITY.md) instead, and don't post it publicly.

The rest of this guide is for maintainers. It covers how changes are made
and what a change must include before it merges.

## Ground rules

- **Best UX and security from the start.** We don't ship a weaker version with a
  plan to harden it later. If a change touches auth, isolation, input handling or
  data integrity, it lands at the final bar or not at all.
- **Every change is a vertical slice.** A pull request delivers one working
  capability end to end: code, tests, docs and, for UI, its loading, empty and
  error states. `main` stays deployable after every merge. Unfinished user-facing
  features stay behind a flag until they are complete.
- **Decisions are written down.** A change that makes or reverses an
  architectural decision adds or supersedes an ADR in [`docs/adr/`](docs/adr/).

## Workflow

1. Open an issue or discussion for anything larger than a small fix, so the
   approach can be agreed before you write it.
2. Branch from `main` and run `make hooks` once per clone. Maintainers and
   coding agents are expected to commit with the hook enabled. Linked
   worktrees share the installation, and each runs its own version of the
   checks. Git LFS keeps its checkout, commit, merge and push hooks.
3. Run `make check` before pushing. CI runs the same checks, and the single
   required status check is **CI passed**. A change to documentation alone
   (Markdown, or anything under `docs/`, but not a plugin's files) runs only
   Prettier and `docs/tests`; `scripts/ci/changes.sh` decides.
4. Open a pull request using the template. Describe what changed, why, and how
   you verified it.

Commit messages are free-form. Explain what changed, why, and how it was
verified. No sign-off or CLA is required. Contributions are made under the
project's Apache-2.0 license (see section 5 of [LICENSE](LICENSE)). See
[ADR 0021](docs/adr/0021-license-and-contribution-model.md) for the
contribution model.

## Toolchains

| Area       | Tools                                                                        |
| ---------- | ---------------------------------------------------------------------------- |
| Python     | uv workspace, Ruff (lint and format), mypy `--strict`, pytest                |
| TypeScript | pnpm workspace, TypeScript (strict), ESLint (type-checked), Prettier, Vitest |
| Rust       | cargo workspace, rustfmt, Clippy (warnings are errors), cargo test           |
| CI         | GitHub Actions, actionlint, zizmor                                           |

Python code supports 3.12 and later. Servers run 3.14, and CI tests both.

The hook checks a snapshot of the Git index, including partially staged
files. A staged Python change runs Ruff lint and format on the changed
files, incremental mypy on the same targets as CI, and Python 3.12 imports
for changed workspace modules. The tools use the worktree's installed
dependencies, so keep dependency manifests and lockfiles staged together
when changing them. The first import check uses uv to provision
`.venv-py312/` from the lockfile. Later commits reuse it. To reproduce an
import failure, run
`scripts/check-imports-py312 -- cli/src/iki_cli/preflight.py` with the
reported file. `make check-python` imports every workspace module on 3.12
before running the other Python checks, including on machines that usually
run a newer interpreter.

Staged web files run Prettier. Project-wide TypeScript and typed ESLint run
in `make check-web`. Fix the reported files, stage the corrections, and
retry the commit.

CI runs the Python tests in shards side by side, whole test files at a time
(`scripts/ci/pytest_shards.py`). Each kind of test has shards of its own with
only the setup it needs: `browser` (marked `built_web_app`) the built web app
and browsers, `worker` (marked `worker_e2e`) iki-worker, and `plain` neither.
Every shard builds the shared WASM for rules and the standalone modules included in its checkout.
3.12 runs the plain tests; 3.14 runs every kind. The **Python tests (summary)**
job fails if any test ran in no shard or in two, and combines the shards'
coverage. To run a shard locally:
`uv run pytest -p pytest_shards --shard plain:2/4`. Shards are balanced by
`scripts/ci/test-durations.json`; when they drift apart, replace it with the
`test-durations` artifact of a recent run, and add shards to the matrix in
`.github/workflows/ci.yml` when the suite outgrows them.

Before running Python browser tests, run `pnpm install --frozen-lockfile`
and `make web test-wasm test-browsers`. Both `make check` and `make check-python`
install the project's locked Chromium, Firefox, and WebKit and run the browser
tests. The gate checks both web builds, their manifest assets, and axe-core.
On Linux, `uv run --locked playwright install-deps chromium firefox webkit`
installs the browsers' system libraries.

`make test-wasm` builds the shared module for the browser and Python, then
runs the checkout's standalone module builders (`scripts/build-<name>-wasm.sh`).
Each standalone crate and builder needs a matching lock under `plugins/*/data/`.
For `<name>.sha256`, the gate verifies the delivered Python module's SHA-256
and bundled Rust notice. For `<name>.wasm.sha256`, it checks the plugin and
packaged copies against each other and delegates canonical-platform digest
verification to the builder.

Start Docker for the test suite's pinned PostgreSQL and SeaweedFS containers.
If you provide test services, set `IKI_TEST_DATABASE_URL` and/or
`IKI_TEST_S3_URL`. The S3 service must accept the credentials in
`server/tests/conftest.py`.

For a partial local pytest run without browsers, use
`IKI_SKIP_BROWSER_TESTS=1 uv run pytest`. The full gate (`IKI_GATE=1`) and
CI require browsers. A gate failure lists each unexpected pytest skip. When a
new capability or opt-in suite warrants a skip, record its exact reason and
the tests it applies to in `scripts/ci/pytest_gate_skips.py`.

## Dependencies

- Lockfiles (`uv.lock`, `pnpm-lock.yaml`, `worker/Cargo.lock`) are committed and
  CI installs from them exactly.
- Nothing released less than 3 days ago is locked. pnpm and uv won't resolve
  such versions; for cargo, pin an older one with
  `cargo update -p NAME --precise VERSION`. `make check-deps` checks every
  lockfile.
- pnpm never runs dependency install scripts unless they are explicitly allowed
  in `pnpm-workspace.yaml`.
- GitHub Actions are pinned by full commit SHA, with the version in a comment.
  Dependabot proposes updates after a 7-day cooldown.
- Before adding a dependency, check its license is compatible with Apache-2.0
  and that it is actively maintained.
