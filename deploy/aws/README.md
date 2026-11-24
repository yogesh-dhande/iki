# Iki on AWS

Reference OpenTofu modules for running Iki in your own AWS account: `modules/` (network, cluster,
database, storage, access, secrets, vm-tier, fsx), `bootstrap/` for the state's bucket and key,
`production/` putting them together with the chart's production values, `scripts/` for secrets,
database roles, recovery and teardown, and `tests/` for the live test.

What each module makes, the steps from an empty account to Iki serving, what the infrastructure
promises, checking it (`make check-infra`, `make check-aws-live`) and what to know before you rely
on it are in [Deploy on AWS](https://ikisci.org/guides/deploy-on-aws/). Restoring and recovering
from a lost region are in
[Restore a deployment from a backup](https://ikisci.org/guides/restore-from-a-backup/).
