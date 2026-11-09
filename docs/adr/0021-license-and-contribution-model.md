# 0021. Apache-2.0 license, maintainer-only contributions for now, one reviewed change per commit

- Status: Accepted
- Date: 2026-11-09

## Context

Iki is open source and is meant to run inside enterprises, whose legal
teams review licenses before adoption. Enterprises also write and keep
their own plugins. The history of `main` should be easy to read and bisect.

The project is in early development. Its architecture is still settling,
and nobody is set up to review outside pull requests yet.

## Decision

- Iki is licensed under **Apache-2.0**. Its explicit patent grant is
  familiar to enterprise legal teams and permits use, modification and
  private plugins.
- **Pull requests are accepted only from maintainers (people with write
  access) for now.** A workflow (`.github/workflows/external-prs.yml`)
  closes PRs from anyone else with a note pointing to issues, which stay
  open to everyone. Dependabot PRs are allowed.
- Maintainers do **not** sign a Contributor License Agreement (CLA) and do
  **not** add a Developer Certificate of Origin (DCO) `Signed-off-by` line.
  Contributions are made under Apache-2.0 itself (its section 5 covers
  contributions submitted to the project).
- Opening the project to outside contributors needs a new ADR that
  supersedes this one. That ADR should also decide again whether to
  require a CLA or DCO.
- Commit messages are **free-form**. Each one describes what changed, why,
  and how it was verified. No conventional-commit prefixes are required.
- **Each commit is equivalent to one reviewed PR**: a vertical slice that
  passes full CI on its own, includes its tests and docs, and leaves
  `main` deployable.

## Consequences

- Positive: a permissive, widely approved license removes an adoption
  blocker.
- Positive: no review load from outside PRs while the architecture is
  still changing, and no implied promise to review them.
- Positive: maintainers face no paperwork or sign-off tooling.
- Positive: `main`'s history reads as a list of complete, reviewable
  changes, and any commit can be deployed or bisected.
- Negative: outside fixes have to come in as issues, which is slower for
  people who already have a patch.
- Negative: the auto-close workflow uses `pull_request_target`, which runs
  with a token for this repo. It is safe only because it never checks out
  or runs PR code, and it must stay that way.
- Negative: without a CLA, the project cannot relicense contributed code
  later without contributors' consent.
- Negative: without DCO sign-off, there is no per-commit statement of
  origin. We rely on Apache-2.0's contribution terms.
- Negative: free-form messages cannot drive automated changelog tools that
  expect structured prefixes, so release notes take manual effort.
