# 0220. Dev seeds run on product plugins

- Status: Proposed
- Date: 2026-11-25

## Context

The dev cluster supplies the data behind the plate assay, plasmid prep,
lot release and core facility guides. Several unrelated checks start inside
the example LIMS import. The sequencing checks also borrow a sample type
from the author template. Removing either example can therefore remove
science coverage from an otherwise working installation.

## Decision

Use a product lock for the ordinary dev cluster. Give each scientific
scenario its own inputs using installed product types. Run the seed on a
fresh product cluster in CI and check for the expected results, including
the signed records returned to the requesting projects. Keep the source
datasets available from the checkout so a lab can repeat the worked cases.

Use a second CI cluster for checks that need the author template or a fake
outside service. Its explicit fixture overlay adds the plugins, settings,
compute bindings and cluster resources those checks use. Build the server
and workers from the same effective lock in each cluster.

## Consequences

The two runs exercise the same scientific scenarios and the same database
restore procedure. Only fixture runs need the fake LIMS and FoldCloud
services and their credentials. Running the product seed from an empty
database also catches accidental dependencies on earlier fixture records.
