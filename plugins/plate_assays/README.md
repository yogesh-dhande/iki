# Plate assays

Map an absorbance export to a 96- or 384-well plate, calculate a relative IC50
or EC50, and sign the result. The result and its source readings have stable
record IDs that another experiment can reference.

## Set up review

Install `iki/plate_assays` with its declared dependencies and bind
`iki/plate_assays.assay-tools` to a backend worker. Apply the dose-response
starter in the assay project to choose Research or Regulated QC, assign lab
roles and run a requested assay on example data. Projects without a starter use
the plugin's deployment setting `review_flow`, which defaults to `research`.
Each result keeps the flow selected when it was created.
[Run a dose-response plate assay](https://ikisci.org/use-cases/dose-response-plate-assay/)

## Run an assay

1. Create an **Assay plate**, choose its size and open the **Plate map** tab.
   Assign samples and controls by well. Record replicate numbers and the
   concentrations of samples in a series. **Assign a dilution series** fills
   an empty block from its starting well, nominal concentration, fold dilution
   and number of concentrations. Replicates occupy parallel rows or columns.
   If the assay answers a request, select the submitted analysis request.
2. Attach a BMG LABTECH MARS endpoint CSV, exported with the absorbance display
   set to OD. Choose **Preview** and inspect the measured and assigned wells.
   Resolve missing, repeated and unassigned wells, then commit the preview.
   The plate read links to the original file and each well's measurement.
   [Export settings and public example file](data/reader.md)
3. Run **Fit a dose-response curve** on the committed plate read and select
   the sample. Choose IC50 or EC50 according to the assay. To exclude a well,
   enter its coordinate and a reason on one line, such as
   `A4: bubble noted at acquisition`. Open the result's **Curve** tab to inspect
   each reading, the excluded wells, parameters, endpoint and approximate
   95% confidence limits. [Worked synthetic example](data/curve-example.md)
4. In research mode, sign off the recorded result. In regulated QC, the analyst
   submits it, an independent reviewer signs it and a different member of QA
   releases it. Open a signature to verify the revision signed. A requested
   result is delivered to the asking project when its final signature is complete.

The fit uses the individual raw absorbance readings for the selected sample.
Control wells remain available for inspection. Extrapolated midpoints carry
an above-range or below-range qualifier. An unreportable curve retains its
readings and the reason an estimate was unavailable.
[Assay design decision](../../docs/adr/0188-a-plate-read-keeps-the-wells-behind-a-fitted-endpoint.md)
