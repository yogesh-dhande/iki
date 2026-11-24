# Run a first dose-response plate assay

Use the **Dose-response** starter to receive a sample, map a plate reader file to its wells,
review an IC50 and return the signed result to the project that requested it. The example uses
synthetic absorbance readings. You can follow them from the original CSV to the released result.
In the seeded development deployment, the **Research plate assays** and **QC plate assays**
projects each contain a completed example.

## Set up the two projects

Sign in as an admin of the assay service project with edit access to the requesting project.
Use two active projects in the same registry with sharing enabled. If the service already takes
requests, its intake settings must allow **Analysis request** from this requester. On a new
service, the starter configures that intake. The deployment needs the `iki/plate_assays` plugin,
its dependencies and a worker bound to `iki/plate_assays.assay-tools`.
[Deploy a plugin](../../deploy/config-template/README.md#how-a-change-reaches-a-deployment)

Use the project UUIDs for `ASSAY_PROJECT` and `REQUESTER_PROJECT`. For the role assignments,
use each person's direct, active membership ID from `GET /api/v1/projects/{project_id}/members`.
Give the coordinator and analyst at least Editor access. The reviewer and QA signer each need at
least Viewer access. For Regulated QC, assign different people as analyst, reviewer and QA.
The CLI uses your SDK sign-in or the `IKI_URL` and `IKI_TOKEN` environment variables.
[Signing in](../../sdk/python/README.md#signing-in)

```bash
iki starter list --project "$ASSAY_PROJECT"

FLOW=regulated_qc    # Set to research for one sign-off.
options=(
  dose-response --version 1.0.0 --project "$ASSAY_PROJECT"
  --requester-project "$REQUESTER_PROJECT" --flow "$FLOW"
  --role "iki/example_lims.coordinator=$COORDINATOR"
  --role "iki/plate_assays.analyst=$ANALYST"
  --role "iki/plate_assays.reviewer=$REVIEWER"
)
if [[ "$FLOW" == regulated_qc ]]; then
  options+=(--role "iki/plate_assays.qa=$QA")
fi
iki starter preview "${options[@]}"
iki starter apply "${options[@]}"
```

Preview shows the example records to create and roles to grant. Apply returns the IDs of the
submitted request, tube, sample, plate and reader file. Repeating the same setup reuses these IDs.
To change flow, preview and apply again with the original requester. Existing results finish
under their original flow. A fit still running when the flow changes may need to be run again.

## Receive the tube and read the plate

1. **Coordinator** — In the assay project's **Requests → Sent to us**, open **Example dose-response
   assay (synthetic)** and select **Receive what it sent**. Enter who handed over the example tube,
   choose **Start receiving**, and enter the tube's `TUB-…` identifier in **Scan each tube**.
   Check that the line is matched and choose to keep the tube in custody without a storage
   position. Select **Complete receiving**. The completed receipt records that the tube was
   received unplaced.
2. **Analyst** — Open **Example MARS dose-response plate (synthetic)** and its **Plate map** tab.
   Check the plate's request and the sample assigned to A1–A8. The concentrations double from
   1 nM at A1 to 128 nM at A8.
   A9 is the blank and A10 the reference.
3. Choose **Preview reader file**. The synthetic MARS CSV has ten readings on the 484 nm
   absorbance channel. Check the well, sample or control, file row and reading for each. Choose
   **Commit measurements** once all ten match. The resulting plate read links to the source file
   and ten individual well measurements. [Example reader data](../../plugins/plate_assays/data/reader.md)
4. Run **Fit a dose-response curve** with the committed plate read and the sample from the
   request. Choose **IC50** and enter `A4: Simulated dispensing error` as the example exclusion.
   On the result's **Curve** tab, check that A4's 8 nM reading is still visible with its reason
   and author. Seven sample wells contribute to the fit. Check the IC50 in nM, its range
   qualifier and approximate 95% confidence interval. For these synthetic data, the IC50 is
   **12.34 nM**, within the measured range, with an approximate 95% interval of **10.65–14.30 nM**.

## Sign and return the result

Signing requires the organization's electronic-signature certification to be recorded in Iki.
Each person signs in again to sign the revision on screen. [Electronic signatures](../adr/0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md)

| Flow         | Who signs and what they check                                                                                                                              |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Research     | The assay reviewer checks the recorded fit and the A4 exclusion, then sets **Signed off**.                                                                 |
| Regulated QC | The analyst signs **Submitted**. An independent reviewer checks the readings and exclusion and signs **Independently reviewed**. QA signs **QA released**. |

In Regulated QC, the reviewer and QA must also be independent of anyone who edited the recorded
values. Correcting the notes on a submitted or reviewed result requires a reason and returns
it to **Recorded** for fresh signatures. To change an exclusion, run a new fit on the committed
read and review that result.

In the requesting project, open **Requests → We sent** and select the same request. After the
final signature, the **Results** section shows the released revision. Open the result and choose
**Check signatures** to verify who signed each revision.
