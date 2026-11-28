# Plate assays

Read absorbance or normalized activity measurements onto a 96- or 384-well
plate in a named reader format, calculate a relative IC50 or EC50, and sign
the result. The result and its source readings have stable
record IDs that another experiment can reference.

For release testing, enter the analytical procedure ID and revision on the assay
plate before previewing its file. The committed read and each fitted result
retain those identifiers for comparison with the approved release criterion.

## Set up review

Install `iki/plate_assays` with its declared dependencies and bind
`iki/plate_assays.assay-tools` to a backend worker. Fits are reviewed under
core's result review (`iki/core.result_review@1`). In the assay project, apply
the **Dose-response plate assay** starter (`iki/plate_assays.dose_response`) in
Project settings → Starters to choose Research (suggested) or Regulated QC, and
grant the shared lab roles (Analyst, Reviewer, Sample and result QA) to its
members. The analyst needs Editor access. Reviewers and QA need at least Viewer
access. The lab can then
use its own plate layouts, materials and reader files. For a training run
with published apigenin and daidzein activity measurements, load the
[Tox21 tutorial](../../examples/dose_response/README.md) in its own projects.

An Org Admin can instead set the deployment's default in Admin → Review
procedures, with a reason, for every project that hasn't chosen. Until one of
them chooses, a fit can't be recorded: the run is refused with 409
`procedure_not_chosen`. Each result keeps the flow it was recorded under.

| Flow                                                                                    | Who signs                                                                                                                     |
| --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Research                                                                                | A reviewer who didn't create or edit the result signs it off.                                                                 |
| Regulated QC                                                                            | Whoever made it signs its submission, an independent reviewer signs its review and someone in QA who did neither releases it. |
| [Run a first dose-response plate assay](../../docs/guides/first-dose-response-assay.md) |

## Run an assay

1. Create an **Assay plate**, choose its size and open the **Plate map** tab.
   Assign samples and controls by well. Record replicate numbers and the
   concentrations of samples in a series. **Assign a dilution series** fills
   an empty block from its starting well, nominal concentration, fold dilution
   and number of concentrations. Replicates occupy parallel rows or columns.
   If the assay answers a request, select the request the project's service received.
2. Choose the plate's **reader file format** and attach the export. Iki reads
   two formats, each a declared subset with a version recorded on every read:
   a BMG LABTECH MARS absorbance endpoint CSV in OD (`bmg_mars_absorbance/1`),
   and Iki's plate matrix of normalized activity in percent
   (`iki_plate_matrix/1`). Choose **Preview** and inspect the measured and
   assigned wells and the export's run header. Resolve missing, repeated and
   unassigned wells, then commit the preview. The plate read links to the
   original file, its format and each well's measurement, and the plate's
   layout is then fixed. To read the same plate again, at another time or
   wavelength, attach that export and commit it as the plate's next read. An
   export already committed to the plate is refused.
   [Formats, export settings and public example files](data/reader.md)
3. Run **Fit a dose-response curve** on the committed plate read and select
   the sample. Choose IC50 or EC50 according to the assay. The fit method is
   `4PL-OLS`, and each result records it with its version, `4PL-OLS/1`.
   [What the method supports](data/methods.md) To exclude a well,
   enter its coordinate and a reason on one line, such as
   `A4: bubble noted at acquisition`. Open the result's **Curve** tab to inspect
   each reading, the excluded wells, parameters, endpoint and approximate
   95% confidence limits. [Source potencies and worked flavonoid fits](data/curve-example.md)
4. Under Research, a reviewer signs off the recorded result. Under Regulated QC,
   the analyst submits it, an independent reviewer signs it and a different
   member of QA releases it. Open a signature to verify the revision signed. A
   requested result is delivered to the asking project when its final signature
   is complete. It's reviewed under the assay project's choice for fits, which
   the project also applies to its own.

The fit uses the individual measurements in their source units for the selected sample.
Control wells remain available for inspection. Extrapolated midpoints carry
an above-range or below-range qualifier. An unreportable curve retains its
readings and the reason an estimate was unavailable.
[Assay design decision](../../docs/adr/0188-a-plate-read-keeps-the-wells-behind-a-fitted-endpoint.md)

A lab that has qualified its own method writes it in its own plugin. Its
results are assay results under the same review. They
record the lab's method, such as `acme/5PL-WLS/1`.
[A lab's own method](data/methods.md#a-labs-own-method)
[Named fit methods](../../docs/adr/0248-a-dose-response-fit-is-a-named-versioned-method.md)
