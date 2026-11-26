# 0192. Lot release testing pins a specification and issues a signed certificate

- Status: Accepted
- Date: 2026-11-23
- Refines: [0126](0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md),
  [0162](0162-a-lot-is-a-projects-record-of-an-entrys-material.md),
  [0171](0171-a-types-records-export-as-files-iki-writes-from-its-fields.md) and
  [0179](0179-a-corrected-result-goes-back-for-review.md)
- Amended: 2026-11-26 — specification contexts and release cycles (see Amendment)

## Context

A lot's certificate must show which specification applied and which measurements supported
the decision. An out-of-specification observation can be attributable to an assignable
laboratory cause. A documented investigation and signed invalidation preserve that
observation along with the repeat used for disposition.
[FDA guidance on investigating OOS results](https://www.fda.gov/media/158416/download)

## Decision

The QC release plugin uses a project-owned specification schedule per material. A version
declares one dated set of procedures and acceptance criteria. QA signs it before publication
adds it to the schedule, and that signature locks the version. A new version takes effect
after earlier ones. A release case pins the schedule and the version in effect on its
testing date. The database enforces one schedule per material and one case per lot within
the project, including when two actions run concurrently.

The case title starts with the lot number printed on its physical label so QA can match
the container to the case in the list. The testing date follows the number. The same lot
number identifies the tested batch on the certificate.
[WHO model certificate of analysis](https://www.who.int/publications/m/item/trs1010-annex4)

The testing action records the measured value and whether it meets the selected criterion.
A result may come from a manually reported measurement or a reviewed plate-assay endpoint
on that physical lot. An OOS value opens an investigation in the same transaction. The
original observation is immutable. A repeat cites the prior attempt and its signed, evidenced
invalidation. A confirmed or unresolved exception blocks a passing certificate.

The research flow needs one sign-off. The regulated QC flow requires the analyst's signed
submission, an independent review and a separate QA release. The case fixes the flow at
opening. Corrections to a submitted or reviewed record return it to Recorded with a reason.
Terminal signatures lock an investigation decision or certificate. QA approval of a
specification is the authorization to put a controlled version into effect in either flow.

The certificate action seals the case and assembles the full attempt sequence, pinned
specification, observed values and signed investigation decisions. The record export reads
the signed revision's data and makes the existing electronic signatures available with it.
Lot QA uses the issued certificate as evidence when setting the lot's disposition. Supplier
certificates continue to be kept on the lot.

## Consequences

Explicit test and investigation selections make the scope of the certificate visible. The
stored attempt sequence checks that nothing was omitted. Approval and publication are
separate recorded steps. The teaching example covers numeric inclusive criteria and a
single release case per lot and project. Product-specific protocols for resampling,
rework, qualifying qualitative tests or repeated commercial release can extend this design.

## Amendment. Specification contexts and release cycles

One schedule per material and one case per lot meant that a lab couldn't keep release and
shelf-life acceptance criteria apart (ICH Q6A §2.2), or test a lot again at its retest
date or after a rework, without losing the case it had.

**A schedule has a context.** `context` names which approved specification the schedule
holds for the material: `Release` by default, or `Shelf life`, a market or a product
grade. A project has one schedule per material and context. Effective dates replay within
each context, and a case pins its schedule's context.

**A case is one cycle.** A lot's first case against a schedule is cycle 1. To test it
again, `open_release` takes the latest case as `previous` and a written reason, and opens
the next cycle. The database keeps one case per lot, schedule and cycle. The previous case
is marked `superseded`, checked against the revision the run read, so two retests can't
both follow one cycle. A superseded case takes no more tests. Its attempts,
investigations and any certificate are unchanged. If it has no certificate, the new
cycle needs the signed conclusion of every out-of-specification result in it. Opening a
cycle never sidesteps an open investigation. A later cycle is never tested before the
one it follows. A case's title is its lot number, context and cycle, then its testing
date, such as `NaCl-24-017 · Release cycle 2 · 2026-11-26`.

**The lot's material is the one its type declares.** `open_release` reads the lot type's
declared material field, as stability does, instead of a fixed list of field names. Any
lot type in `iki/lots` can be released, including a strain isolate, which is its own
material.

The lot's disposition remains Lot QA's (ADR 0211). A retest cycle is evidence for the
lot's re-release, not the re-release itself.
