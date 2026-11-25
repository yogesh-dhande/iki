# Run a first dose-response plate assay

Use the **Dose-response** starter to receive a sample, map a plate reader file to its wells,
review an IC50 and return the signed result to the project that requested it. The example uses
published apigenin and daidzein activity measurements against firefly luciferase. Follow each well
from the normalized activity CSV to its signed result. [PubChem AID 1224835](https://pubchem.ncbi.nlm.nih.gov/bioassay/1224835).
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
  dose-response --version 1.1.0 --project "$ASSAY_PROJECT"
  --requester-project "$REQUESTER_PROJECT" --flow "$FLOW"
  --role "iki/example_lims.coordinator=$COORDINATOR"
  --role "iki/plate_assays.analyst=$ANALYST"
  --role "iki/plate_assays.reviewer=$REVIEWER"
)
if [[ "$FLOW" == regulated_qc ]]; then
  options+=(--role "iki/plate_assays.qa=$QA")
fi
iki starter preview "${options[@]}"
iki starter apply "${options[@]}" --plan "$PLAN"    # The plan the preview printed.
```

Preview shows the example records to create and roles to grant, and ends with its plan. Apply
applies that plan: if what applying does changed since the preview, nothing is applied, and it
prints what applying does now and its new plan, to review. Apply returns the IDs of the
submitted request, target enzyme sample and tube, two compound tubes, plate and reader file. Repeating the same setup reuses these IDs.
To change flow, preview and apply again with the original requester. Existing results finish
under their original flow. A fit still running when the flow changes may need to be run again.

## Receive the tubes and read the plate

1. **Coordinator** — In the assay project's **Requests → Sent to us**, open **Tox21 firefly luciferase inhibition** and select **Receive what it sent**. Enter who handed over the shipment,
   choose **Start receiving**, and scan the three labelled tubes for luciferase, apigenin and daidzein.
   Check that all three lines are matched. Under **Put away**, set each tube to **Keep in my
   custody**, then select **Complete receiving**. The completed receipt records that the tubes were
   received without a storage position, and **Storage** lists them under **Received, not yet put
   away** until someone puts them away.
2. **Analyst** — Open **Tox21 firefly luciferase inhibition** and its **Plate map** tab.
   Rows A–C are apigenin replicates and D–F are daidzein replicates. Columns 1–11 span
   0.017–115.2 µM. Column 12 contains the active (ataluren) and inactive (caffeine) controls.
3. Choose **Preview reader file**. Check the 72 signed percent-activity readings and the
   controls against their file rows. Choose **Commit measurements**. The committed read links
   to the input CSV and every individual measurement. [Plate layout and source data](../../plugins/plate_assays/data/curve-example.md).
4. Run **Fit a dose-response curve** once for each compound on that read. Choose **IC50**
   and leave exclusions empty. The **Curve** tab shows all 33 contributing wells per compound.
   Apigenin gives **10.21 µM** and daidzein **20.45 µM**. Compare the approximate 95%
   interval for each with the source replicate potencies in the worked example.

## Sign and return the result

Signing requires the organization's electronic-signature certification to be recorded in Iki.
Each person signs in again to sign the revision on screen. [Electronic signatures](../adr/0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md)

| Flow         | Who signs and what they check                                                                                                                             |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Research     | The assay reviewer checks the replicate curves and controls, then sets **Signed off**.                                                                    |
| Regulated QC | The analyst signs **Submitted**. An independent reviewer checks the readings and controls and signs **Independently reviewed**. QA signs **QA released**. |

In Regulated QC, the reviewer and QA must also be independent of anyone who edited the recorded
values. Correcting the notes on a submitted or reviewed result requires a reason and returns
it to **Recorded** for fresh signatures. To change an exclusion, run a new fit on the committed
read and review that result.

In the requesting project, open **Requests → We sent** and select the same request. After the
final signature, the **Results** section shows the released revision. Open the result and choose
**Check signatures** to verify who signed each revision.
