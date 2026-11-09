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
2. Branch from `main`.
3. Run `make check` before pushing. CI runs the same checks, and the single
   required status check is **CI passed**.
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

## Dependencies

- Lockfiles (`uv.lock`, `pnpm-lock.yaml`, `worker/Cargo.lock`) are committed and
  CI installs from them exactly.
- pnpm refuses package versions less than 3 days old and never runs dependency
  install scripts unless they are explicitly allowed in `pnpm-workspace.yaml`.
- GitHub Actions are pinned by full commit SHA, with the version in a comment.
  Dependabot proposes updates after a 7-day cooldown.
- Before adding a dependency, check its license is compatible with Apache-2.0
  and that it is actively maintained.
