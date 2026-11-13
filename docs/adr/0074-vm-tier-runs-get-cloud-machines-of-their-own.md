# 0074. VM-tier runs get cloud machines of their own

- Status: Accepted
- Date: 2026-11-12
- Refines: [0009](0009-compute-tiers-and-templates.md),
  [0041](0041-compute-templates-and-bindings.md),
  [0061](0061-container-tier-runs-get-pods-of-their-own.md),
  [0062](0062-warm-pods-run-one-job-after-another-and-start-each-clean.md)

## Context

Some runs need a machine the cluster doesn't have: eight A100s for a
structure prediction, say, or a terabyte of memory for an assembly. Keeping
nodes like those in the cluster, idle between runs, costs more than most
sites will pay. Starting one for each run, and giving it back afterwards,
costs only the hours used, and spot capacity cuts that again.

A machine like that is outside the cluster's NetworkPolicies and PodSecurity
rules. It runs code Iki didn't write, on hardware the site pays for by the
hour, in an account it shares with other systems. In a GxP setting, an
auditor asks what ran, on what machine, from which image, and who could
have touched it. And the people waiting want to know why it hasn't started.

## Decision

**The provisioner starts an EC2 instance for each queued vm-tier run.**
Templates with `provisioner: aws_ec2` name the instance type, region,
machine image, subnets, security groups and instance profile. The same
`iki-server provision` that starts container-tier pods (ADR 0061) does it:
one loop, two fleets, the same rules for which template, how many machines,
`max_concurrent`, warm machines (ADR 0062), backoff and giving up.

**Each machine is locked down from the moment it starts.**

- It has no public address, and its metadata service needs IMDSv2 session
  tokens, with a hop limit of one, so its containers can't reach it. The
  boot script also drops containers' traffic to the metadata address.
- Its disk is encrypted (with the template's KMS key, if it names one), and
  holds the template's scratch space plus 40Gi for the system.
- Shutting it down from inside terminates it.
- It's tagged with `iki.ikisci.org/deployment` (the deployment's
  `vmTier.owner`), its launch, template and environment, and while it's a
  run's, the run, project and team, for cost tools.

**The machine runs the environment's image with Iki's worker, as a pod
would.** Its boot script copies `iki-worker` out of Iki's own signed worker
image, pinned by its digest, and starts `iki-worker host` as a service. That
pulls the environment's image (signing in to ECR with the machine's own
role) and runs it with the worker mounted in, running `iki-worker start
--once`. The container is the container tier's pod, in Docker:

- a non-root user, no capabilities, no privilege escalation
- a read-only root, with only the scratch directory writable
- exactly the template's CPUs and memory, with no swap, and its GPUs
- an environment file the token is in only until the container's made

When the container ends, the machine shuts down, and so terminates.

**Each machine holds one credential, made for it alone,** as each pod does:
a token that only its worker may register with, once, within an hour. What
it may run comes from its launch. Every launch, the instance it got (type,
market, zone, subnet, machine image), and how it ended are in the audit log.

**Iki manages only its own machines, and its IAM policy can say so.** The
provisioner lists, tags and terminates only instances carrying its
deployment tag. Its policy can require that tag on every instance it starts
and every one it ends, so deployments sharing an account never touch each
other's. The policy it needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "StartOnlyTaggedMachines",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": [
        "arn:aws:ec2:*:*:instance/*",
        "arn:aws:ec2:*:*:volume/*",
        "arn:aws:ec2:*:*:network-interface/*"
      ],
      "Condition": {
        "StringEquals": {
          "aws:RequestTag/iki.ikisci.org/deployment": "iki-production"
        }
      }
    },
    {
      "Sid": "StartFromTheTemplatesResources",
      "Effect": "Allow",
      "Action": "ec2:RunInstances",
      "Resource": [
        "arn:aws:ec2:*::image/*",
        "arn:aws:ec2:*:*:subnet/*",
        "arn:aws:ec2:*:*:security-group/*"
      ]
    },
    {
      "Sid": "TagThemAsTheyStart",
      "Effect": "Allow",
      "Action": "ec2:CreateTags",
      "Resource": "arn:aws:ec2:*:*:*/*",
      "Condition": { "StringEquals": { "ec2:CreateAction": "RunInstances" } }
    },
    {
      "Sid": "EndAndReadOnlyItsOwn",
      "Effect": "Allow",
      "Action": ["ec2:TerminateInstances", "ec2:GetConsoleOutput"],
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/iki.ikisci.org/deployment": "iki-production"
        }
      }
    },
    {
      "Sid": "LabelItsOwnWithTheirRun",
      "Effect": "Allow",
      "Action": ["ec2:CreateTags", "ec2:DeleteTags"],
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/iki.ikisci.org/deployment": "iki-production"
        },
        "ForAllValues:StringEquals": {
          "aws:TagKeys": ["iki.ikisci.org/run", "iki.ikisci.org/project", "iki.ikisci.org/team"]
        }
      }
    },
    {
      "Sid": "SeeMachines",
      "Effect": "Allow",
      "Action": "ec2:DescribeInstances",
      "Resource": "*"
    },
    {
      "Sid": "GiveThemTheirRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::123456789012:role/iki-vm-worker",
      "Condition": {
        "StringEquals": { "iam:PassedToService": "ec2.amazonaws.com" }
      }
    },
    {
      "Sid": "ResolveAwsMachineImages",
      "Effect": "Allow",
      "Action": "ssm:GetParameters",
      "Resource": "arn:aws:ssm:*::parameter/aws/service/*"
    }
  ]
}
```

A template's KMS key needs the usual grants for EBS on it, and the first
spot machine in an account needs EC2's spot service-linked role. The
machines' own role (the instance profile) needs only to read the images
they run: `AmazonEC2ContainerRegistryReadOnly` for ECR, or nothing for a
public registry.

**What EC2 reports is reconciled with what Iki launched,** as with pods:

- A machine that's terminated ends its launch. Its worker is revoked, and
  its job is interrupted and retried under the action's policy.
- **Spot:** two minutes before EC2 reclaims a spot machine, it posts a
  notice to the machine's metadata. `iki-worker host` watches for it and
  stops the container within 100 seconds: the worker checkpoints, if the
  action can (ADR 0069), and reports the job interrupted. The launch ends
  `reclaimed`, and the run is retried, resuming from its checkpoint.
- **No capacity:** when EC2 has none of the instance type, the provisioner
  tries each of the template's subnets (so each zone), then on-demand if
  the template falls back to it. If none has any, the queued runs say so
  ("Waiting for a machine: EC2 has no p4d.24xlarge free (…). Iki tries
  again shortly.") and it tries again, waiting longer each time.
- **Refused:** an error that won't go away by itself (no permission, an
  image or subnet that doesn't exist) fails the environment's queued runs
  with a message for an administrator, rather than leaving them waiting.
- **The machine gave up:** when `iki-worker host` can't pull the image, it
  says why on the machine's console, and the provisioner reads it back into
  the launch.
- A machine that never registered in time is terminated. So is any machine
  carrying the deployment's tag with no live launch, and any stopped one.

## Consequences

- A deployment turns the tier on with `vmTier` in the chart: an owner name,
  an address machines reach the server at, and the provisioner's IAM role.
  The templates in `compute.yaml` name the rest.
- Machines reach the server, object storage and the egress proxy through
  their subnets: security groups and the VPC's routing are the site's,
  since they're outside the cluster's NetworkPolicies. Runs reach other
  hosts only through the egress proxy, at `vmTier.egressProxyUrl`.
- A machine per run costs a boot and two image pulls: a few minutes, which
  is small next to the hours runs on these machines take. Warm machines
  (`min_warm`) keep some ready.
- Volumes on vm-tier machines come with managed volumes (commit 36).
- Only AWS. Other clouds would be fleets of their own, with the same rules.
