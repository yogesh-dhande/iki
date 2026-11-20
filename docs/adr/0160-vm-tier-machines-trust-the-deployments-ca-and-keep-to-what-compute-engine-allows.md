# 0160. VM-tier machines trust the deployment's CA, and keep to what Compute Engine allows

- Status: Accepted
- Date: 2026-11-19
- Refines: [0074](0074-vm-tier-runs-get-cloud-machines-of-their-own.md),
  [0109](0109-google-cloud-natively.md)

## Context

A VM-tier machine's worker reaches the server at `vmTier.serverUrl`, often
an internal load balancer in the machines' own network. Many enterprises
give an internal service a certificate from their own CA, and the live
Google Cloud test does (deploy/gcp/tests). The worker runs inside the
environment's image, which trusts only that image's public roots, so it
couldn't register: backend workers have `backendWorkers.caSecret` for this,
the VM tier had nothing.

Machines' boot script also drops every packet from a run's containers to
the cloud's metadata address, so a run can't take its machine's
credentials (ADR 0074). On EC2 the resolver is elsewhere (the VPC's `.2`
address). On Compute Engine the resolver _is_ the metadata address
(169.254.169.254, port 53), so the drop left a run's containers unable to
resolve any name, the server's included.

The live test's machines are e2-smalls standing in for GPU machines, and
Compute Engine refused every one: a standard machine was asked to stop for
host maintenance (`onHostMaintenance: TERMINATE`), which GPUs need, and E2
machines allow only when Spot.

The provisioner finds its machines, disks and images by the label
`iki-deployment=<owner>`, and deletes those no launch or build claims.
deploy/gcp puts that same label on everything of the deployment's, its
cluster's nodes included, so the provisioner tried to delete the nodes as
strays. Only its IAM condition, which limits it to names starting with
`<owner>-`, stopped it.

## Decision

**`vmTier.caSecret` names a Secret with the CA certificates machines'
workers trust besides their own roots.** The provisioner alone mounts it
(`IKI_RUNS_VM_CA_FILE`), and each machine's host configuration carries the
certificates. `iki-worker host` writes a bundle of the machine's roots and
those certificates, readable by anyone (mode 0644, set on the file whatever
the service's umask, since the worker isn't root), and bind-mounts it
read-only into the worker's container as `/etc/iki/ca-certificates.crt`,
with `SSL_CERT_FILE` naming it. The server refuses a file that holds no PEM certificate or more than
8 KiB at start: the certificates go in every machine's boot script, so they
are the deployment's own few, not a bundle of public ones. It reads the
file once, as X.509 certificates in PEM blocks, and refuses any other
block (a private key beside them, as load balancers take them), a block
that isn't whole, or a certificate that can't be read. Machines get those
certificates written again as PEM, never the file itself, so nothing else
in it reaches their metadata or their runs. Without it, nothing changes.

**On Compute Engine, a run's containers may reach the metadata address on
port 53 alone.** Two `ACCEPT` rules (UDP and TCP) are inserted above the
drop; everything else to the address, its credentials included, is still
dropped. EC2's script is unchanged.

**A machine that can't keep its runs from the metadata server starts
nothing.** On both clouds the boot script inserts the drop (and, on Compute
Engine, the two exceptions), then checks each rule is in DOCKER-USER
(`iptables -C`). If any can't be made or isn't there, the machine writes
`IKI-HOST-ERROR unsupported` to its console and shuts down before it signs
in to a registry, pulls an image or starts `iki-worker host`; the
provisioner ends the launch as refused. A machine image whose Docker has no
DOCKER-USER chain (iptables off, or another firewall) can't run the VM
tier.

**A standard machine stops for host maintenance only if it has GPUs.**
Without GPUs it's migrated live (`MIGRATE`), so its run goes on, and E2
machines start. Spot machines still stop, as Compute Engine requires.

**The provisioner's own resources carry their own label, not only the
deployment's.** An instance is its own if it has a launch label
(`iki-launch`) and is named `<owner>-`; a disk or image, if it has a
setup build's (`iki-setup`). Whatever else carries the deployment's label
is left alone, whoever made it.

## Consequences

- A deployment whose server certificate is its own CA's works on the VM
  tier as it does for backend workers, without rebuilding environment
  images to add the CA.
- The CA's certificates are in each machine's metadata (or user data), as
  public certificates may be; the 8 KiB limit keeps the boot script within
  Compute Engine's and EC2's limits.
- On Compute Engine a run can send DNS queries to the metadata server, as
  any process on the machine can; it still reaches no other metadata path.
- A CPU machine may move hosts while it runs; GPU machines still stop, and
  their runs are attempted again elsewhere.
- Labels say whose a resource is only together with the provisioner's own
  label and name; the IAM condition stays as a second guard.

## Alternatives considered

- **Bake the CA into each environment's image.** Every plugin's image
  would need each deployment's CA, and first-party images couldn't be used
  as published.
- **Give containers another resolver** (Google's public DNS, through the
  NAT). Machines have no internet route (ADR 0155), and private zones,
  such as the internal load balancer's name, resolve only at the metadata
  server.
