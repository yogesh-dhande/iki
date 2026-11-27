# QC release testing

Open a release case on a physical lot, evaluate its results against the specification in
force, and issue a certificate of analysis with Iki electronic signatures. The case keeps
the specification and lot revisions, every original result, every repeat, and every signed
investigation. The lot's existing Disposition and Certificates of analysis fields are where
Lot QA records a decision and keeps its supporting documents, including supplier certificates.

## Set up a project

Install `iki/qc_release` and its dependencies. A project's Admin chooses the review flow
in **Project settings → Starters → Lot release testing** and assigns its lab roles there.
Release Testing QA approves specifications in either flow. Research investigations and
certificates need one QC reviewer's sign-off. Regulated QC needs a QC analyst, an
independent QC reviewer and a separate Release Testing QA signer.
A project that hasn't chosen uses the deployment's `review_flow` setting (`research` by
default), so a regulated certificate is only ever issued under an explicit choice. Each
release case keeps the flow it was opened with, and its investigations and certificate
follow it when the project's choice changes
([ADR 0240](../../docs/adr/0240-a-project-chooses-its-release-stability-and-verification-review.md)).
Release Testing QA is its own grant: it doesn't give the Lot QA authority to release or
reject the lot itself.

Set `certificate_issuer` to the manufacturer's name, address and telephone, as each
certificate states them (ICH Q7 §11.43). A regulated certificate can't be prepared without
it.

Register the material and a physical lot with its lot number. Create a **Specification
schedule** for the material in each **context** the lab specifies it for. The default is
`Release`; others might be `Shelf life`, a market or a product grade (ICH Q6A §2.2). Each
context has its own versions and effective dates, and a project has one schedule per
material and context. Create a **Specification version** in that schedule with an
effective date, a controlled source, and one criterion per attribute. Every criterion names
its method and its kind of result (ADR 0244):

- A **numeric** criterion has a reporting unit and an inclusive lower limit, upper limit or
  both, such as an assay of 99.0–100.5%.
- A **conforms** criterion states its requirement, such as `Meets the requirements` for an
  identification test or `White crystalline powder` for appearance. It has no limits or
  unit. ICH Q6A lists such tests beside quantitative ones.

List two identification tests as two attributes, as monographs do (`Identification A`,
`Identification B`).

Release Testing QA signs **Approved for publication** on the version. Run **Publish an
approved specification version** on the schedule and that signed version. The first version
can establish a historical baseline. Publication appends versions and retains all earlier
ones. A successor starts after its predecessor and after the latest testing date any release
case has pinned from the schedule, so publishing never changes which version a case was
tested under. The deployment's `successor_effective_dates` setting says how soon:

| Policy             | A successor may start                                                                                                                                       |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `future` (default) | After today (UTC).                                                                                                                                          |
| `same_day`         | Today, for a version effective on approval, unless a case already tested under its predecessor today.                                                       |
| `backdated`        | On an earlier date, as when importing a history of approved versions. Its publication says why, in **Why it took effect in the past**, which the run keeps. |

[Effective dates decision](../../docs/adr/0241-a-specifications-successor-takes-effect-after-every-pinned-testing-date.md)

## Test, investigate and issue

1. On the lot's **QC release** tab, open release testing with the material's schedule and
   the testing date. The schedule must be for the material the lot's type declares, so
   any lot type works; a strain isolate is its own material. The action selects the most
   recent approved, published version effective on that date. The case retains its lot number and the schedule and version
   revisions the run read.
2. Record each criterion's value with its declared method and unit. For a conforming
   criterion, say whether it conforms instead, and describe what was seen. Iki records a
   result that does not conform as out of specification, like a number outside its limits,
   and opens its investigation. For a manual observation, attach the source laboratory
   report if available. The plate-assay path uses a signed,
   reportable, in-range IC50 or EC50 result for the same physical lot and pins its revision.
   Record the same analytical procedure ID and revision on the plate and the
   specification criterion. Set the criterion method to the fit method the
   specification accepts, by its name and version: `4PL-OLS/1` for plate_assays' own
   fit, or a lab's locally qualified method under its own name, such as
   `acme/5PL-WLS/1`. A result fitted by another method, or another version of it, isn't
   evidence for the criterion, so a lab's method counts only where the specification
   names it. Signed endpoints qualify as evidence when the procedure, its revision and
   the fit method all agree.
   Regulated release requires a QA-released plate-assay result.
3. Open the investigation created with an out-of-specification observation. Set its
   **Investigator**, which assigns it to that person. In Regulated QC the investigator must
   hold the analyst role, and neither the investigator nor the submitter can review or
   release it.
   - **Testing during the investigation.** Write the investigation's **Testing plan** under
     the lab's approved procedure, for example a retest of the same preparation. Then record
     each test it plans with **Investigational** as the purpose and the open investigation
     selected. An investigational result is kept in the case's sequence and pinned to the
     plan's revision. It opens no second investigation and replaces nothing. Once the
     investigation is submitted or signed, it takes no more tests.
   - **The conclusion** is one of three:
     - **Confirmed:** the original result stands.
     - **Invalidated:** needs the assignable cause, supporting evidence and the signed
       disposition. Only a signed invalidation lets a **Reportable** repeat replace the
       original. Select that original attempt and investigation explicitly when recording
       the repeat.
     - **Inconclusive:** no assignable cause was found, either way. The original result
       stands in the lot's disposition, as for Confirmed (FDA OOS guidance, §V).
       Confirmed, inconclusive and unsigned investigations prevent a passing certificate. QA
       can place the lot on hold or reject it using the lot's disposition. The original
       measurement remains visible.
4. Prepare the certificate with every attempt, investigational ones included, and each
   signed investigation. Iki checks the selections against the case's attempt sequence
   and confirms every criterion has a passing result. It then seals the case and writes
   a certificate record with two parts:
   - **Certificate:** what the certificate states (ICH Q7 §11.4). That's the issuer, the
     lot number with its made, retest and expiry dates, the specification, and each
     criterion's method, limits or requirement, and final reportable result. A Research
     certificate is titled as one and says it was issued on one reviewer's sign-off, not
     released by a quality unit.
   - **Evidence:** everything behind the certificate. That's every attempt with its
     analyst and evidence, the full pinned criteria and each signed investigation.
     It stays internal to the lab.
5. Sign the certificate record through the selected flow. It's internal: it keeps the
   evidence for audit and has no export.
6. Run **Issue the certificate of analysis** on the signed record. The certificate of
   analysis holds only what the certificate states. It doesn't name the tests,
   investigations or people behind it. A research certificate's reviewer issues it, and
   Release Testing QA issues a regulated one. That signature is its own.
   Use **Export** on the issued certificate to download it as signed HTML or
   machine-readable JSON, which is what the lab sends. A recipient can check the
   signature against the file they hold, and any edit fails that check. The HTML can be
   printed to PDF and attached to the lot alongside any supplier certificates. The
   certificate record names the certificate by its SHA-256 inside Iki only. Lot QA signs
   the lot's disposition through its existing workflow.

## Test a lot again

Each case is one **cycle** of a lot's testing against one specification context. To test
the lot again, run **Open lot release testing** with the lot's latest case for that
schedule as **Previous** and the reason, for example its retest date or a rework under a
deviation. Iki then:

- opens the next cycle;
- marks the previous case as followed, so it takes no more tests;
- keeps the previous case's attempts, investigations and any certificate unchanged.

If the previous case has no certificate, include the signed investigation of each of its
out-of-specification results. A lot isn't tested again around an open investigation. Two
retests can't both follow the same cycle. A lot's first cycle against a shelf-life
schedule is independent of its release cycles. The lot's disposition, including
re-release after a retest, stays with Lot QA in `iki/lots`.

## Public teaching example

The [specification fixture](tests/data/sodium-chloride-specification.json) uses two criteria
from the historical USP Sodium Chloride Stage 6 Harmonization, official December 1, 2015.
The assay limits are 99.0–100.5% NaCl on the dried basis using the monograph's potentiometric
titration. Loss on drying has an upper limit of 0.5% by USP &lt;731&gt;. The material is
PubChem CID 5234. The fixture is a teaching excerpt. Every local lot, observation, report
and investigation in the walkthrough is synthetic.
[USP monograph](https://dev.uspnf.com/sites/default/files/usp_pdf/EN/USPNF/sodium-chloride.pdf),
[PubChem CID 5234](https://pubchem.ncbi.nlm.nih.gov/compound/5234)

The example retains a failed 98.7% assay, requires a signed investigation documenting the
assignable cause, then records a 99.9% repeat and a 0.2% loss-on-drying result. The certificate
keeps both assay attempts and references the investigation.
[FDA guidance on investigating OOS results, sections IV–V](https://www.fda.gov/media/158416/download)

A versioned starter would be useful for a lab's approved specification and review-role
setup. A lab administrator should establish the actual controlled methods, limits and
authorized signers before using this procedure for commercial disposition.
