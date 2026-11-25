# 0217. Releasing a quarantined file keeps its finding

- Status: Accepted
- Date: 2026-11-25
- Refines: [0216](0216-files-wait-for-a-scan.md), [0176](0176-the-app-is-laid-out-around-a-sidebar-and-what-you-can-read.md)

## Context

A scanner can identify a lab's legitimate file as infected. The people who
work with it need a way to ask for review, and an auditor needs to see what
the scanner found and who allowed use afterwards.

## Decision

Quarantine is a hold on the immutable source. Keep the bytes in private
storage under their existing access and retention controls. Record the
scanner's finding and the audit event together. All file references to
the source observe the same hold.

A deployment administrator can assign the organization role Quarantine
Admin. Its holder can list held decisions, inspect the finding and release
an infected decision by its ID with a nonblank reason. This role grants
no project membership or download privilege. A release writes a separate
immutable record and audit event with the actor, time, reason and decision.
The file keeps its infected result and exposes the release as its basis
for use. A pending inspection or incomplete scan cannot be released as a
false positive.

Quarantine Admins review in Admin → Security → Quarantine, a group of the
admin area's own beside Compliance, as GitHub and Slack keep security apart.
It lists what's held, or what was released, with each source's files, their
projects and who added them. Its decision page shows the finding, the
release and its reason. A file's page names its hold and links Quarantine
Admins to that page; file lists and pickers mark a held file, and a picker
can't choose one.

A release is a security operator's exception, with the authenticated
identity and a reason. It does not sign or approve a scientific record.
Record approval and electronic signature workflows still apply to any
subsequent use in a regulated record.

## Consequences

A later review can reconstruct both decisions. A release applies to aliases
of the same source and does not grant access to a different registered
version or change the histories of files and records.
