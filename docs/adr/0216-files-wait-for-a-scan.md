# 0216. Files wait for a scan before use

- Status: Accepted
- Date: 2026-11-25
- Refines: [0036](0036-files-content-addressed-uploads.md), [0044](0044-run-results-are-written-all-or-nothing.md)

## Context

A file can enter a project through an upload, an instrument bucket, a worker or
a bundle. Some instrument files are hundreds of gigabytes. A hash identifies
content but says nothing about what a malware scanner found in it.

## Decision

Keep the scan decision beside the immutable blob. All names for that blob
share its decision. A registered object is identified by its pinned version
and scanned from the approved bucket. Temporary worker artifacts, checkpoints
and uploaded bundle archives have a decision on their own upload, preserving
their existing retention periods. Worker results take the ordinary file path.

Until a decision exists the state is pending, including for older data.
The task runner streams the verified object to the deployment's scanner.
ClamAV is the default. The deployment can install its own scanner adapter.
A clean decision requires checking the complete stream. An outage leaves
the object pending for retry. The result names the scanner, signatures,
time and finding or reason. Updating signatures affects future scans.
This decision does not schedule recurring scans of completed objects.

The deployment sets its maximum scan size. An oversize file gets an explicit
`not_scanned` decision with the reason `too_large`. The default is to hold it.
An operator may configure a size exception that allows use while preserving
the original label and the policy decision. Other incomplete inspections
remain held. Media type and extension cannot exempt a file.

Check permission to see a file before revealing its state. A person can
follow the progress of an upload or registration. Attaching a new upload
waits for the decision. Metadata stays visible while content is held.
Downloads, viewer URLs, file fields, run inputs and results, parsers, restored
checkpoints and bundle exports require a usable decision. The same rule applies
when reading earlier revisions or preparing an already queued job.
Check a bundle archive before opening it and establish local evidence for
its individual files before applying records that use them.

Run clamd in its own Deployment behind a cluster-internal service. Only the
components that perform scans may connect. FreshClam can use the deployment's
signature mirror. Keep network policy default-deny and use a pinned official
image in development and CI. EICAR exercises the real scanner.

## Consequences

Existing content can spend time waiting after an upgrade. Background work
recovers outstanding scans, and consumers can retry once a decision is
available. Scanning adds an object read and scanner capacity to plan for.
A person reviewing a large file can distinguish an operator's exception
from a successful inspection.
