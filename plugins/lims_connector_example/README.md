# LIMS connector example

`iki/lims_connector_example` is the pattern to copy for keeping Iki in sync with another system. Its manifest declares the Protein dependency and the required `lims_host` setting. The connector owns the import and write-back actions, their triggers and the external API client. See [ADR 0211](../../docs/adr/0211-lots-inventory-samples-and-requests.md).

The external protein ID is the key in both directions. Import matches `lims_id` so a subsequent change updates the same protein. The nightly trigger uses the last run's watermark. When someone renames a protein in Iki, the write-back action patches that external record and retains what it sent. Repeating the write sends the same ID and name.

When adapting the example, replace the fictional API's listing, pagination and update methods with the other system's equivalents. Configure its HTTPS hostname in `lims_host`, bind the worker's `LIMS_TOKEN` secret and scope action egress to that host. The external record link and the client should use the same hostname.

Set `lims_host` in the deployment settings and map the worker secret `iki__lims_connector_example.LIMS_TOKEN` to a token issued by that host. The Python worker uses the standard library.
