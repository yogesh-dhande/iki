# 0155. Workers and runs reach only the server, object storage and the egress proxy

- Status: Accepted
- Date: 2026-11-19
- Refines: [0053](0053-runs-reach-the-internet-through-an-egress-proxy.md)

## Context

A run's command is plugin code that Iki didn't write. A backend worker
runs it in the worker's own pod, so the command shares the worker's
network. A container-tier run gets a pod of its own. ADR 0053 sends what
they fetch from the internet through the egress proxy, which checks each
host against the action.

Workers and run pods also reach the server and object storage, directly.
A NetworkPolicy names addresses, not hosts, and a cloud load balancer or
storage service answers at public addresses the chart doesn't know. So
the chart let workers and run pods reach any public address on port 443,
for the server and for object storage. That is most of the internet, and
it doesn't pass the proxy.

## Decision

**Workers and run pods reach DNS, the server, object storage, the egress
proxy and what `networkPolicy.egress.workers` adds. Nothing else.**

**The operator says where the server is.** `networkPolicy.egress.server.to`
is empty by default. It lists the load balancer's own addresses, or the
ingress controller's pods when that runs in the cluster. The pods and
namespaces in `networkPolicy.ingressFrom` are added, since an ingress
controller there is one. Its address blocks aren't: they are where
traffic to web pods comes from, such as a load balancer's front ends, not
where workers connect. Run pods with `containerTier.serverInCluster`
reach the web pods directly, and need no server peers.

**And where object storage is.** `networkPolicy.egress.objectStorage.to`
keeps public addresses by default, for web pods, task runners and
maintenance. With workers or run pods, it lists the store's own
addresses.

**The chart refuses what reaches too far.** With network policies on and
backend workers or the container tier, the chart doesn't render when:

- there are no server peers, unless only run pods with `serverInCluster`
  need the server;
- an address block that workers or run pods may reach is wider than a /8
  (IPv4) or a /32 (IPv6). That covers `server.to`, `objectStorage.to` (with
  `files.bucket`) and each rule's `to` in `egress.workers`. A `/0` with
  `except` entries is refused too: what's left is still most of the
  internet;
- such a block is in IPv6's `::/16`, where IPv6 writes IPv4 addresses
  (`::ffff:0:0/96` is all of them), and some network plugins match it as
  IPv4;
- a rule in `egress.workers` lists no peers, which reaches anywhere.

Each message names the value and the block, and says what to list.

A /8 is the widest private range (`10.0.0.0/8`). A /32 is what one
organization is usually allocated in IPv6. Cloud storage ranges are far
narrower: S3's prefix list, or Google's `private.googleapis.com`
(`199.36.153.8/30`).

**The reference deployments name both.** On Google Cloud
(`deploy/gcp/production`), the server is the load balancer's own address,
external or internal, and Cloud Storage is `private.googleapis.com`. On
AWS (`deploy/aws/production`), S3 is its gateway endpoint's prefix list,
read on each apply, and an internal ALB is in the private subnets. An
internet-facing ALB's addresses change and aren't known to OpenTofu, so
with backend workers the operator sets `networkPolicy.egress.server.to`.

## Consequences

- The egress proxy is the only way from a run to the rest of the
  internet, for both tiers.
- Turning on backend workers or the container tier takes one more value
  (two, with files). The chart says which, and the config template shows
  an example.
- A NetworkPolicy names addresses, not buckets. A storage service's
  addresses serve every bucket on it, so a command could reach another
  bucket there with credentials of its own. A deployment confines that
  with its provider's endpoint policy: an S3 VPC endpoint policy that
  allows only its own buckets, or VPC Service Controls around the project
  on Google Cloud. A load balancer's address can likewise serve other
  sites behind it.
- Addresses that change, such as an internet-facing ALB's, are the
  operator's to keep current. An ingress controller named by a selector
  doesn't change. AWS adds ranges to S3's prefix list now and then: apply
  OpenTofu again and upgrade the release.
- The dev cluster's end-to-end check runs from a worker pod: it reaches
  the web pods and object storage, and not Keycloak or Postgres.

## Alternatives considered

- **Names instead of addresses** (Cilium's or Calico's DNS-based
  policies). They aren't part of Kubernetes' NetworkPolicy, and not every
  cluster runs them.
- **The server and storage through the egress proxy too.** Every file a
  run reads or writes would pass through it, and a worker's own traffic
  to the server isn't a run's: the proxy's credentials are per attempt.
