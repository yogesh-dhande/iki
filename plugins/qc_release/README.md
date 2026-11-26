# QC release testing

Open a release case on a physical lot, evaluate its results against the specification in
force, and issue a certificate of analysis with Iki electronic signatures. The case keeps
the specification and lot revisions, every original result, every repeat, and every signed
investigation. The lot's existing Disposition and Certificates of analysis fields are where
Lot QA records a decision and keeps its supporting documents, including supplier certificates.

## Set up a project

Install `iki/qc_release` and its dependencies. Set `review_flow` to `research` or
`regulated_qc` in the plugin deployment settings. Give project members the QC Analyst,
QC Reviewer and Release Testing QA lab roles as appropriate. Research investigations and
certificates need one reviewer sign-off. Regulated QC needs an analyst, independent reviewer
and separate QA signer. Iki retains each case's original flow when the setup changes.

Register the material and a physical lot with its lot number. Create one **Specification
schedule** for the material. Create a **Specification version** in that schedule with an
effective date, a controlled source, and one criterion per attribute. Every criterion names
its method and unit and has an inclusive lower limit, upper limit or both.

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
   the testing date. The action selects the most recent approved, published version
   effective on that date. The case retains its lot number and the schedule and version
   revisions the run read.
2. Record each criterion's value with its declared method and unit. For a manual observation,
   attach the source laboratory report if available. The plate-assay path uses a signed,
   reportable, in-range IC50 or EC50 result for the same physical lot and pins its revision.
   Record the same analytical procedure ID and revision on the plate and the
   specification criterion. Set the criterion method to the assay's fit version,
   for example `4PL-OLS/1`. Signed endpoints qualify as evidence when all three
   identifiers agree.
   Regulated release requires a QA-released plate-assay result.
3. Open the investigation created with an out-of-specification observation. Record the
   findings and conclusion. Invalidation needs the assigned cause, supporting evidence
   and signed disposition. If signed as invalidated, select that original attempt and
   investigation explicitly when recording a repeat. Confirmed failures and unresolved
   investigations prevent a passing certificate. QA can place the lot on hold or reject
   it using the lot's disposition. The original measurement remains visible.
4. Prepare the certificate with every attempt and each signed investigation. Iki checks
   the selections against the case's attempt sequence, confirms every criterion has a
   passing result, captures all the pinned evidence, and seals the case.
5. Sign the certificate through the selected flow. Use **Export** on the issued record
   to download the signed HTML or machine-readable JSON. The HTML can be printed to PDF
   and attached to the lot alongside any supplier certificates. Lot QA signs the lot's
   disposition through its existing workflow.

One project holds one case per lot in this example. A lab that releases the same lot again
under a retest or rework SOP needs its own disposition and case-series policy.

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
