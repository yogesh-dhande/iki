# Experiment records

Keep the versions of the notebook entry, protocol, material usage and results
that belong to one experiment. The lab continues to write and retain its
notebook in the ELN.

## Record the work

1. Create an **Experiment**. Enter its title, the ELN entry ID or URL, and
   its revision. Record the external protocol ID and the exact revision used.
2. Select the samples, add dated observations and attach the raw evidence
   or a rendition exported from the ELN.
3. From a lot's amount ledger, choose **Draw**, enter the amount and unit, and
   select the experiment under **Used in**. A successful draw debits that lot
   and records the movement on the experiment. The lot's ordinary expiry,
   QA and balance checks apply.
4. Run **Capture results for review** for the experiment. Choose the existing well
   measurements, curve results or other records to include. Capture records
   the numbered versions the run read. An experiment with a direct observation
   or attachment can be closed without a separate result record. Repeat capture
   after a value edit or additional draw so the signer sees the current evidence.
5. Inspect the experiment's evidence and sign using the project's assigned
   roles. A research witness signs once. In regulated QC, the author submits,
   an independent reviewer signs, then QA releases. A correction to a
   submitted or reviewed QC record needs a reason and a new round of signatures.

A later change to a result or registry entry leaves the captured revision
available for review. Keep the external ELN revision accessible to witnesses
or attach a rendition to the experiment. [Evidence and sign-off](../../docs/adr/0189-an-experiment-pins-the-evidence-its-witness-signs.md)

## Set up

Set the plugin's `review_flow` to `research` (default) or `regulated_qc`
in the deployment settings. Assign the author, reviewer and QA lab roles in the
project that owns the experiment. Bind the plugin's backend environment to a
worker for the capture action.
