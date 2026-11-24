# 0210. Delegated actions require a shared lab role

- Status: Accepted
- Date: 2026-11-24
- Amends [0116](0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)
- Refines [0073](0073-unattended-agents-act-for-a-person-by-delegation.md)

## Decision

For a service account acting on a person's behalf, each lab role used to authorize
an action must belong to both identities in the project where the role is checked.
Each identity gets its roles from its own direct and team memberships. If a step
accepts either Analyst or QA, a person holding only Analyst and a service account
holding only QA have no shared role for that step.

This rule applies to state transitions, the roles exposed to guards, receiving,
material draws and other lab-role decisions. The token's scopes, project grant
and each identity's required project role also apply. Workflow approvals and
signatures that require a person in the web app still require that sign-in.

A delegated change records the person and the agent under the delegation in the
existing audit trail. Starting a receipt records both at creation, and reopening
that same receipt creates no second start event. [ADR 0072](0072-agents-changes-name-the-person-and-the-agent.md)
