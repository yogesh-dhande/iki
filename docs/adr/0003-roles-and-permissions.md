# 0003. Fixed org and project roles with one permission service

- Status: Accepted
- Date: 2026-11-09

## Context

Iki controls access to sensitive data and to compute that costs real money.
Running a quick calculation on a backend worker costs little. Running a GPU
job on a cloud VM can cost hundreds of dollars. Enterprises also need staff
who can manage compute or read the audit log without being able to edit
science data. Users reach Iki through the UI, the REST API, the Python SDK,
MCP and an SSE stream, and every path must enforce the same rules.

## Decision

Iki has a fixed set of roles.

- Org-level roles:
  - **Org Admin**: manages the organization, teams and settings.
  - **Compute Admin**: manages compute (templates, pools, workers, builds,
    volumes, cost).
  - **Auditor**: reads the audit log.
- Project-level roles, each including the one before it:
  - **Viewer**: reads entities, runs and views.
  - **Editor**: edits entities and runs actions on the backend and client
    tiers.
  - **Runner**: also runs actions on the container and VM tiers.
  - **Admin**: manages the project and its members.
- Org Admins can restrict which compute tiers and compute templates each
  team may use.
- Service accounts receive the same roles as people. They authenticate with
  OAuth client credentials.
- One central permission service makes every authorization decision. It is
  called on every API, MCP and SSE path. A full matrix of authorization
  tests covers every role against every operation.

## Consequences

- Positive: a small, fixed set of roles is easy to explain, audit and test
  exhaustively.
- Positive: spending is gated by role. A user must be a Runner to launch
  container or VM jobs, on top of budgets and approvals (ADR 0009,
  ADR 0013).
- Positive: one permission service means one place to fix a bug and one
  place to test.
- Negative: enterprises cannot define custom roles. Finer control comes
  only from team-level tier and template restrictions.
- Negative: every new endpoint or tool must go through the permission
  service and be added to the test matrix. This is a deliberate cost.
