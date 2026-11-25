# Core facility request examples

These files accompany the [Requests plugin](../../plugins/requests/README.md) and the dev cluster’s two worked facility requests.

For the protein production example, register the yeast alcohol dehydrogenase 1 sequence from
[P00330.fasta](P00330.fasta). Send a Research request for 1 mg and upload
[P00330-production.txt](P00330-production.txt) in the core as the delivery report for the
training lot and 0.8 mg delivery. The requested quantity, delivered quantity,
and lot are synthetic. The protein and sequence come from the UniProtKB
entry under its attribution license.
[UniProtKB P00330](https://www.uniprot.org/uniprotkb/P00330/entry),
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

For sequencing, register a training tube named CEY652 and send it with a
Regulated QC request for long-read sequencing. In the core, receive it and
use [ERR13376132-delivery.txt](ERR13376132-delivery.txt) for the tube's report with public
run accession `ERR13376132`. The accession and sample identifier refer to
a published yeast dataset. The request, custody, and signatures are training
events. [NCBI Sequence Read Archive](https://www.ncbi.nlm.nih.gov/sra/ERX12747051)
