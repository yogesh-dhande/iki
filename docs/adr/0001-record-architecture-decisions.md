# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-11-09

## Context

Iki starts with many foundational decisions already made: tenancy, storage,
the plugin model, compute, security and deployment. These decisions shape
every later commit. Contributors, plugin authors and the enterprise teams
who operate Iki need to know what was decided and why, long after the
original discussion is gone.

Iki is open source. Its design discussions must live somewhere public,
versioned and reviewable, next to the code they govern.

## Decision

We record each significant architecture decision as an Architecture
Decision Record (ADR): a short Markdown file in `docs/adr/`.

- The format is MADR-lite: a title, status, date, and the sections
  Context, Decision, Consequences, and optionally Alternatives considered.
  See `0000-template.md`.
- Files are named `NNNN-kebab-title.md`, numbered in order. Numbers are
  never reused.
- One ADR records one decision, or one tightly related group of decisions.
- An ADR is added or changed in the same commit as the work it governs, or
  in a commit of its own, and goes through normal review.
- Status values are `Proposed`, `Accepted`, and `Superseded by NNNN`.
- Once accepted, an ADR's decision is not rewritten. To change a decision,
  write a new ADR and mark the old one `Superseded by NNNN`. Typo fixes and
  clarifications that do not change meaning are fine.

## Consequences

- Positive: the reasoning behind Iki's design is public and searchable.
  New contributors can learn the system's constraints without asking.
  Reversing a decision forces a written argument against the original one.
- Positive: reviews of large changes can point to an ADR instead of
  repeating the debate.
- Negative: writing ADRs takes time, and they can go stale if people change
  the code without writing a superseding record. Review must catch this.
