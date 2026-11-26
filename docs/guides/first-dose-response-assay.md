# Run a first dose-response plate assay

Choose the review flow for your assay project. You can then run a plate using
your lab’s own materials and reader file, or follow the public Tox21 experiment
below in dedicated training projects.

## Configure your assay project

Install `iki/plate_assays` with its declared dependencies and bind
`iki/plate_assays.assay-tools` to a backend worker. As a project admin, open
**Project settings**, choose **Set it up from a starter** and select
**Dose-response plate assay**.
Choose **Research** for one sign-off or **Regulated QC** for analyst submission,
independent review and QA release. The analyst needs at least Editor access.
A reviewer or QA signer needs at least Viewer access. Assign different people
to the three regulated QC roles. [Deployment configuration](../../deploy/config-template/README.md)

To apply the same choice from the CLI, set `ASSAY_PROJECT` to the project UUID.
Use direct, active **membership IDs** from `GET /api/v1/projects/{project_id}/members`
for the roles. The CLI uses your SDK sign-in or `IKI_URL` and `IKI_TOKEN`.
[Signing in](../../sdk/python/README.md#signing-in)

```bash
iki starter list --project "$ASSAY_PROJECT"

FLOW=regulated_qc
options=(
  dose-response --version 2.0.0 --project "$ASSAY_PROJECT" --flow "$FLOW"
  --role "iki/plate_assays.analyst=$ANALYST"
  --role "iki/plate_assays.reviewer=$REVIEWER"
)
if [[ "$FLOW" == regulated_qc ]]; then
  options+=(--role "iki/plate_assays.qa=$QA")
fi
iki starter preview "${options[@]}"
```

If the preview reports `Already applied`, setup is complete. If it lists changes,
review them and set `PLAN` to the identifier it prints.

```bash
iki starter apply "${options[@]}" --plan "$PLAN"
```

Applying saves the recipe version, review flow and grants. The project
can still be empty. Reapplying the same choice is safe. To change the flow
for future fits, preview and apply the new choice. Existing results retain
their review requirements. A fit already running may need to be run again.

## Use your lab’s plate

Create an **Assay plate**, enter its procedure and revision, and assign your
samples, replicates, concentrations and controls on the **Plate map** tab.
Attach the reader file, preview the well mapping and commit the measurements.
Run **Fit a dose-response curve**, select the sample and endpoint, then review
the curve and sign according to the project’s flow. If the assay is commissioned
by another project, configure the service’s request intake and link the plate
to the submitted request. [Run an assay](../../plugins/plate_assays/README.md#run-an-assay)

## Load the worked Tox21 experiment

The tutorial uses published apigenin and daidzein activity measurements against
firefly luciferase. Stock volumes and custody events are practice data. The
loader creates dedicated assay and requesting projects in your chosen team.
From an Iki checkout, sign in as someone who can create those projects and
supply the team UUID and **user IDs** of the participants. [Tutorial requirements and source data](../../examples/dose_response/README.md)

```bash
uv run python -m examples.dose_response --team "$TEAM_ID" --flow research \
  --analyst "$ANALYST_USER_ID" --reviewer "$REVIEWER_USER_ID"
```

For regulated QC, use `--flow regulated_qc` and add `--qa "$QA_USER_ID"`.
The assay projects are named **Research plate assays** and **QC plate assays**.
The corresponding requesting projects receive the signed results. Both
completed examples are also available after the development seed runs.
[Published assay](https://pubchem.ncbi.nlm.nih.gov/bioassay/1224835)

## Receive the tubes and read the plate

1. **Coordinator** — In the assay project's **Requests → Sent to us**, open **Tox21 firefly luciferase inhibition** and select **Receive what it sent**. Enter who handed over the shipment,
   choose **Start receiving**, and scan the two compound tubes, apigenin and daidzein.
   Check that both lines are matched. The plate links to the assay project's own luciferase reagent tube.
   Under **Put away**, set each compound tube to **Keep in my
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

After the final signature, sign back in as the tutorial operator. In the requesting project,
open **Requests → We sent** and select the same request. The **Results** section shows the
released revision. Open the result and choose
**Check signatures** to verify who signed each revision.

Once both signed fits have been returned, the Research reviewer or the Regulated QC QA signer
can open the request in the tutorial assay project and set it to **Delivered**. If a compound
still needs a result, the request names it.
