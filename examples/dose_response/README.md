# Run the Tox21 firefly luciferase experiment

Use this exercise to practice receiving compound tubes, fitting a dose-response curve from a
reader file, and returning a signed result to the requester. It uses the published apigenin
and daidzein measurements from PubChem BioAssay 1224835. The requester, stocks and material
draw are training records. For an assay with your lab's own material, configure the project
with the dose-response starter. [First dose-response assay](../../docs/guides/first-dose-response-assay.md)

## Load the training projects

From an Iki source checkout, run `uv sync --locked` and sign in to the deployment with its
matching Python SDK. The operator needs read/write access and permission to create projects
in the chosen team. Choose all projects when authorizing the SDK so it can access the two new
ones. Use a team intended for training. The deployment needs a working file store, the
plate-assays worker environment, and the Plate assays, Protein, Small molecule, Lots,
Inventory, Requests and Samples plugins with their dependencies. [SDK sign-in](../../sdk/python/README.md#signing-in)

Set `TEAM_ID` to the team's UUID. Set `ANALYST_ID`, `REVIEWER_ID` and, for Regulated QC,
`QA_ID` to the participants' **user** IDs. The tutorial creates their project memberships.
The signed-in operator coordinates receipt of the material. In the training service, the
Research reviewer or the Regulated QC QA signer also holds the request role needed to mark
the request Delivered.

```sh
uv run python -m examples.dose_response --team "$TEAM_ID" --flow research \
  --analyst "$ANALYST_ID" --reviewer "$REVIEWER_ID"

uv run python -m examples.dose_response --team "$TEAM_ID" --flow regulated_qc \
  --analyst "$ANALYST_ID" --reviewer "$REVIEWER_ID" --qa "$QA_ID"
```

Choose three distinct signers for Regulated QC. Each invocation creates its own pair of
projects in the selected team.

| Flow         | Assay service         | Requesting project             |
| ------------ | --------------------- | ------------------------------ |
| Research     | Research plate assays | Research plate assay requester |
| Regulated QC | QC plate assays       | QC plate assay requester       |

The command applies starter version 2.0.0 to the training service, grants the coordinator
role, and configures intake from its training requester. It registers the two compound lots
and tubes, sends the request, uploads the source file, maps the plate, and records a single
10 µL practice draw from a 100 µL luciferase stock. The printed JSON identifies both
projects, the request, tubes, plate and input file so you can open the exact records.

If interrupted, run the same command as the same person. It resumes with its local receipt
under `~/.local/state/iki/dose-response/`. Use `--state PATH` to select an explicit SQLite file.
Keep that file if moving to a different workstation. It identifies the run that owns this
project pair and serializes concurrent retries. A same-name project from another run causes
the loader to stop. Repeating a completed load returns its IDs and preserves subsequent
reviews, signatures, and inventory movements.

## Receive, fit and review

1. In the service project, open **Requests → Sent to us → Tox21 firefly luciferase
   inhibition**. Start receiving and scan both compound tubes. Choose **Keep in my custody**
   and complete receiving.
2. Open the plate's **Plate map** tab. The plate reads its file in the Iki plate matrix
   format. Preview the normalized activity CSV and confirm 72
   readings, including six controls. Commit the measurements. Rows A–C contain apigenin and
   rows D–F contain daidzein. Column 12 carries the active and inactive controls.
3. Run **Fit a dose-response curve** for each compound on that read. Select **IC50** and use
   all 33 wells per compound. The pooled fits are approximately **10.21 µM** for apigenin and
   **20.45 µM** for daidzein. [Reader CSV](data/aid-1224835-activity.csv) · [Layout and deposited potency values](data/aid-1224835.json)
4. The reviewer signs off a Research result. For Regulated QC the analyst submits, the
   independent reviewer reviews, then QA releases. Each person signs in to sign their own
   revision. The deployment's electronic-signature certification must be in place.
5. Sign back in as the tutorial operator. In the requesting project, open **Requests → We sent**, select the request and open each
   returned fit under **Results**. Check signatures and the released revision. After both results return, the Research
   reviewer or Regulated QC QA signer can open the request in the service project and set its
   status to **Delivered**.

The measurements come from the NIH/NCATS Tox21 screen and are in the public domain.
The enzyme record uses UniProt P08659, sequence version 1 (UniProt, CC BY 4.0).
See the dataset notes for the deposited substances, replicate potencies, normalization,
control selection and example layout. [Dataset notes](../../plugins/plate_assays/data/curve-example.md) · [PubChem AID 1224835](https://pubchem.ncbi.nlm.nih.gov/bioassay/1224835) · [UniProt P08659](https://www.uniprot.org/uniprotkb/P08659/entry)
