# Mouse strains

Register strains using Mouse Genome Informatics (MGI), follow their JAX catalogue links and find the cryopreserved material from each strain. A genotype record identifies the local animal or embryo group whose genotype was reported. A cryostock represents one physical carrier of embryos or sperm.

## Register a strain

1. In the project's actions, choose **Register a mouse strain from an MGI report**. Upload the headerless, tab-delimited `MGI_Strain.rpt` or an excerpt preserving its three columns. Enter the MGI accession and, if known, the six-digit JAX stock number. The action copies the official designation and retains the file, accession and digest of the matching row. The included two-row report contains C57BL/6J, accession `MGI:3028467`, JAX `000664`. [MGI report catalogue](https://www.informatics.jax.org/downloads/reports/index.html) · [JAX C57BL/6J](https://www.jax.org/strain/000664)
2. You can also create a **Mouse strain** directly. For example, the catalogue strain `B6(Cg)-Tyr<c-2J>/J` has MGI accession `MGI:2163869` and JAX stock `000058`. The MGI accession identifies the strain across projects in the same registry. [MGI strain entry](https://www.informatics.jax.org/strain/MGI:2163869) · [JAX stock 000058](https://www.jax.org/strain/000058)
3. Register **Mouse allele** `MGI:1855985` with symbol `Tyr<c-2J>` and add it to the strain's **Alleles**. The list can contain the subset of catalogued alleles of interest. The strain designation, JAX stock and curated alleles are registry facts. A registrar can correct them with a reason. [MGI allele entry](https://www.informatics.jax.org/allele/MGI:1855985)

## Bank a carrier and trace its use

The following identifiers and observations describe a synthetic local inventory using the public albino B6 strain above.

1. Create a **Mouse genotype** for local subject `Example donor A`. Select its strain, enter the reported notation, for example `Tyr<c-2J>/Tyr<c-2J>`, choose its registered alleles and attach the original genotyping report. Use the notation in the report to preserve zygosity at each reported locus. [MGI catalogue genotype example](https://www.informatics.jax.org/allele/allgenoviews/MGI:1855985)
2. Create a **Mouse cryostock** for each carrier. Supply the kind, strain, actual bank date and any printed barcode. An embryo carrier can record the integer number of embryos originally banked and their genotype if known. A sperm carrier can link to its donor's genotype. Leave the genotype empty when evidence is unavailable.
3. Create a **Mouse cryobox** with the dimensions of the box in use. From **Storage**, put it into an existing freezer rack or shelf and assign each carrier to a position. Scan its barcode or Iki HRID for checkout, return and identification. The location history records moves and custody changes.
4. If the laboratory tracks suspension volume, enter a measured opening amount in µL. For a withdrawal, scan the carrier, record the measured volume and select the resulting record in **Used in**. The ledger shows the remaining volume and the graph links the result to the physical carrier. The embryo count at banking remains a historical observation.
5. Open **Upstream** on the result to trace it to the carrier, its strain, any recorded genotype and the registered alleles. **Downstream** on a strain finds the material and data made from it. The project's **Files** view can filter by a cryostock or genotype holding an original file.

The plugin uses the standard record, storage, amount and graph screens. Its storage dependency is the box type in `iki/inventory`.
