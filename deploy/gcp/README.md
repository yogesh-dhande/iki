# Iki on Google Cloud

Reference OpenTofu modules for running Iki in your own Google Cloud project: `modules/` (kms,
network, logs, cluster, database, storage, secrets, access, artifact-registry, vm-tier, filestore,
binary-authorization), `bootstrap/` for the APIs and the state's bucket and key, `production/`
putting them together with the chart's production values, `scripts/` for secrets, database roles,
recovery and teardown, and `tests/` for the live test.

What each module makes, the steps from an empty project to Iki serving, what the infrastructure
promises, checking it (`make check-infra`, `make check-gcp-live`) and what to know before you
rely on it are in [Deploy on Google Cloud](https://ikisci.org/guides/deploy-on-google-cloud/).
Restoring and recovering from a lost region are in
[Restore a deployment from a backup](https://ikisci.org/guides/restore-from-a-backup/).
