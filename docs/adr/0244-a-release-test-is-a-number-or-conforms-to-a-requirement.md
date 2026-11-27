# 0244. A release test is a number or conforms to a requirement

- Status: Accepted
- Date: 2026-11-26
- Refines: [0192](0192-lot-release-testing-pins-a-specification-and-issues-a-signed-certificate.md)

## Context

A specification version's criteria could only be numbers with inclusive limits. Every
criterion needed a reporting unit, and its approval guard demanded a limit. ICH Q6A §3.2.1
lists tests that aren't numbers in drug substance and drug product specifications:
description, identification, and other qualitative tests whose result meets a requirement
or doesn't. A monograph's "Identification A: meets the requirements" couldn't be
specified, so the lab either left it off the certificate or invented a number for it.

## Decision

**Each criterion says its kind of result.**

- A `numeric` criterion is as before: it has a unit and an inclusive lower limit, upper
  limit or both.
- A `conforms` criterion states its requirement and has neither limits nor a unit.

A rule keeps the two shapes apart. Approval needs every numeric criterion's limit and every
conforming criterion's requirement.

**A conforming result is recorded as conforming or not.** `record_test` takes `conforms` or
`does_not_conform` for such a criterion, with an optional description of what was seen. It
refuses a value or a plate-assay result there, and a conformance answer for a numeric
criterion. A result that doesn't conform is out of specification. It opens the case's
investigation like any other OOS result, keeps its attempt in sequence, and needs a
signed invalidation before a repeat. The release test pins its kind, its requirement, its
result and the observation. The certificate's report carries them and shows the
requirement where a number shows its limits.

**One criterion per attribute stays.** A specification that identifies a material two
ways lists two attributes, as monographs do, rather than two criteria under one name.
Then each attempt chain, repeat and final passing result stays per attribute.

## Consequences

A lab records a specification as its monograph or controlled document states it, with
the same OOS, repeat and certificate controls for every kind of test. Numeric
specifications behave exactly as before.

The plate-assay result remains the one instrument path for a numeric result, and
`iki/plate_assays` remains a dependency. Any other numeric result is entered with its
report. A per-instrument adapter would need a plugin interface for creating another
plugin's release tests, and none exists yet.
