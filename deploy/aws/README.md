# Iki on AWS

Reference OpenTofu modules for running Iki in your own AWS account
([ADR 0097](../../docs/adr/0097-reference-aws-infrastructure-in-opentofu.md)).
They use the AWS provider alone, no third-party modules, so your platform
team can read and qualify every resource they make.

| Module                       | What it makes                                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------------------------------ |
| [`network`](modules/network) | A VPC over two to four zones: public, private and database subnets, NAT gateways, flow logs, endpoints |
| [`cluster`](modules/cluster) | EKS with private Bottlerocket nodes: a system pool, and dedicated pools for runs and GPUs              |

The database, storage, roles for Iki's pods, the VM tier, secrets, and a
production deployment that puts them together come next (roadmap step 48).

## What they promise

- Nothing has a public address but load balancers and NAT gateways. The
  Kubernetes API is private unless you list networks that may reach it.
- Each purpose has its own customer-managed KMS key, rotated yearly.
- Logs are kept: VPC flow logs and every EKS control-plane log, encrypted.
- Pods can't take a node's role (IMDSv2, one hop), and a node's role can
  only join the cluster and pull images.
- NetworkPolicies are enforced (the VPC CNI's network policy agent).
- Plugins' code runs on its own nodes. Pools other than `system` are
  tainted `iki.ikisci.org/pool=<pool>:NoSchedule`. Give backend workers
  and compute templates the pool's node selector and toleration, which the
  cluster module's `node_pools` output lists.

## Checking them

```console
make check-infra
```

It runs `tofu fmt`, `tofu validate` and `tofu test` for each module, then
Checkov's AWS checks. The tests run against a mocked AWS provider, so they
need no account or credentials: they check what would be applied (a
database subnet has no route out, the API isn't open to the internet), not
that AWS accepts it. Each check Checkov skips says why, in the code.

The AWS provider is pinned in each module's `.terraform.lock.hcl`, with
checksums for Linux and macOS.
