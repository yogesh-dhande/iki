# 0019. Monorepo with uv, pnpm and cargo workspaces and hardened CI

- Status: Accepted
- Date: 2026-11-09

## Context

Iki has a Python server, CLI, codegen and SDK; a TypeScript web host,
component library and SDK; and a Rust worker, protocol crate and WASM
parsers. All three share the specs in `spec/` and change together. Each
commit must pass full CI on its own and leave `main` deployable. CI also
builds the signed artifacts enterprises run (ADR 0017), so it is part of
the attack surface.

## Decision

All code lives in one repo, `ikisci/iki`, with one workspace per language.

- **Python**: a uv workspace. ruff for lint and format, mypy in strict mode,
  pytest. Development and server images use Python 3.14. Packages support
  Python 3.12 and newer, and CI tests 3.12 and 3.14.
- **TypeScript**: a pnpm workspace on Node 24 LTS. TypeScript 6.0, ESLint
  10 with flat config and typescript-eslint, Prettier, and Vitest.
- **Rust**: a cargo workspace in `worker/` with a pinned toolchain.
  rustfmt, and clippy with warnings denied.
- **CI**: GitHub Actions.
  - Every third-party action is pinned by commit SHA, not by tag.
  - Each workflow requests the least token permissions it needs.
  - Workflows are linted with actionlint (correctness) and zizmor
    (security issues such as injection or excess permissions).
  - Dependabot updates dependencies for every ecosystem, including the
    pinned actions, after a 7-day cooldown.
- **Dependencies**: all lockfiles are committed and CI installs from them
  exactly. pnpm refuses package versions less than 3 days old
  (`minimumReleaseAge`) and runs no dependency install scripts unless they
  are explicitly allowed, which blunts typosquatting and hijacked releases.
- **Entry point**: `make check` runs exactly what CI runs, and one
  aggregate job ("CI passed") is the only required status check.

## Consequences

- Positive: a spec change and every generated type that depends on it land
  in one reviewed commit.
- Positive: strict linting and type checking from the first commit, so
  there is no later cleanup.
- Positive: SHA pinning and least-privilege tokens protect the pipeline
  that signs releases.
- Positive: testing the oldest and newest supported Python versions catches
  version-specific breakage.
- Negative: contributors need three toolchains installed.
- Negative: CI runs for several ecosystems on every commit, which costs
  time.
- Negative: SHA pins and Dependabot produce a steady stream of update PRs
  to review.
