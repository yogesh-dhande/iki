# 0076. VM-tier machines get environment volumes from EBS snapshots

- Status: Accepted
- Date: 2026-11-13
- Refines: [0074](0074-vm-tier-runs-get-cloud-machines-of-their-own.md),
  [0075](0075-environment-setup-builds-and-managed-volumes.md)

## Context

ADR 0075 fills an environment's volumes with a setup build and gives each
of its runs' pods a read-only copy of the newest version. Runs on the vm
tier, such as AlphaFold on an 8-GPU instance, start EC2 instances of their
own (ADR 0074), and until now could not have these volumes: their runs
waited, saying so.

On AWS, a disk made from an EBS snapshot is lazily loaded from S3: its
first read of each block is slow. For a database of terabytes that every
run reads, that can make each run's first hour many times slower than the
rest. EBS's _fast snapshot restore_ makes disks from a snapshot start fully
loaded in the zones it's enabled in, at an hourly cost for each snapshot
and zone. A gp3 disk's baseline (3,000 IOPS, 125 MiB/s) is also far below
what tools like AlphaFold's MSA search want.

## Decision

**Builds work as they do on Kubernetes, with EBS for the disks and
snapshots.** A build's place is its template's region. Its machine is an
EC2 instance of the template, always on demand (a spot instance taken back
hours into a download would waste them), with an empty, encrypted gp3 disk
for each volume the setup fills, attached as `/dev/sdf`, `/dev/sdg` and so
on, in order of the volumes' names. These disks are kept when the instance
ends; the instance's root disk isn't.

**The provisioner records which disk is which before the script runs.** EC2
names the disks only once they're attached, so the provisioner reads them
from the instance, tags each with its volume (`iki.ikisci.org/volume`), and
records it on the build's launch. The build's worker can lease it only
then. Every disk a script writes can be found and snapshotted, even if
nothing else about the machine survives.

**The machine's host formats and mounts the disks.** `iki-worker host`
waits for each disk to be attached, formats an empty one as ext4, mounts it
writable for the run's user, and binds it into the container where the
environment says. When the container ends, it unmounts them, so each is
whole when snapshotted. The machine image must name EBS disks by their
device, as Amazon Linux's udev rules do; a host that can't find a disk
within five minutes gives up, saying why.

**Once the instance has ended and its disks are detached, each is
snapshotted.** The snapshots carry the deployment's tag, the build's ID and
the volume's name. A template whose volume says `fast_snapshot_restore`
has each completed snapshot made fast to restore in the zones of its
subnets, and the build succeeds only once it is, in every zone: runs then
start at full speed, while the previous version keeps serving them until
then. If EC2 won't enable it yet (accounts can have five such snapshots
per region by default), the build says why and Iki keeps asking. A build whose snapshots aren't ready within a
day of its script's timeout fails. The build's disks are deleted once it
succeeds.

**Machines for runs get a new disk from the newest version's snapshot,
mounted read-only.** Each disk is made with the instance and deleted with
it. The host mounts it read-only (and its journal unreplayed, since it was
unmounted cleanly), and binds it read-only into the container. The version
is in the machine's spec, so warm machines with an older one are replaced
once idle, and in each job's record, as on Kubernetes.

**Templates set the disks' performance.** A template's volume can set
`iops` (3,000 to 80,000) and `throughput` (125 to 2,000 MiB/s, at most a
quarter of the IOPS) for both a build's disks and runs' copies.

**Only the version runs get is kept fast to restore.** Once a newer version
is current, the older one's snapshot stops being fast to restore, which costs
by the hour; it's kept, at the cost of its storage, to roll back to. If the
newer one is retired, the older one is current again and is made fast again,
saying so until it is (runs meanwhile get it, a little slower at first). A
retired version's snapshot is deleted once no machine has a copy. A
failed or cancelled build's disks and snapshots are deleted, as are any
carrying the deployment's tag whose build is gone. Iki looks for these
every five minutes in each region its templates use.

**The provisioner's IAM policy grows by what this needs, and only for its
own disks and snapshots.** Besides ADR 0074's statements, with
`ec2:CreateAction` there also allowing `CreateSnapshot`:

```json
[
  {
    "Sid": "StartFromItsOwnSnapshots",
    "Effect": "Allow",
    "Action": "ec2:RunInstances",
    "Resource": "arn:aws:ec2:*::snapshot/*",
    "Condition": {
      "StringEquals": { "aws:ResourceTag/iki.ikisci.org/deployment": "iki-production" }
    }
  },
  {
    "Sid": "SnapshotItsOwnDisks",
    "Effect": "Allow",
    "Action": "ec2:CreateSnapshot",
    "Resource": "arn:aws:ec2:*:*:volume/*",
    "Condition": {
      "StringEquals": { "aws:ResourceTag/iki.ikisci.org/deployment": "iki-production" }
    }
  },
  {
    "Sid": "MakeOnlyTaggedSnapshots",
    "Effect": "Allow",
    "Action": "ec2:CreateSnapshot",
    "Resource": "arn:aws:ec2:*::snapshot/*",
    "Condition": {
      "StringEquals": { "aws:RequestTag/iki.ikisci.org/deployment": "iki-production" }
    }
  },
  {
    "Sid": "NameItsOwnDisks",
    "Effect": "Allow",
    "Action": "ec2:CreateTags",
    "Resource": "arn:aws:ec2:*:*:volume/*",
    "Condition": {
      "StringEquals": { "aws:ResourceTag/iki.ikisci.org/deployment": "iki-production" },
      "ForAllValues:StringEquals": { "aws:TagKeys": ["iki.ikisci.org/volume"] }
    }
  },
  {
    "Sid": "EndAndSpeedUpItsOwn",
    "Effect": "Allow",
    "Action": [
      "ec2:DeleteVolume",
      "ec2:DeleteSnapshot",
      "ec2:EnableFastSnapshotRestores",
      "ec2:DisableFastSnapshotRestores"
    ],
    "Resource": ["arn:aws:ec2:*:*:volume/*", "arn:aws:ec2:*::snapshot/*"],
    "Condition": {
      "StringEquals": { "aws:ResourceTag/iki.ikisci.org/deployment": "iki-production" }
    }
  },
  {
    "Sid": "SeeDisksAndSnapshots",
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeVolumes",
      "ec2:DescribeSnapshots",
      "ec2:DescribeFastSnapshotRestores",
      "ec2:DescribeSubnets"
    ],
    "Resource": "*"
  }
]
```

The template's KMS key needs EBS's usual grants for the provisioner's role,
since disks made from its snapshots are encrypted with it.

## Consequences

- AlphaFold-sized tools run on the vm tier with their data on a disk of
  their own, at full speed from the start when the template asks, and every
  result says which version of the data it read.
- Fast snapshot restore costs about $0.75 an hour for each snapshot and
  zone it's enabled in: about $1,600 a month for a template in three zones.
  Only the current version of each environment's volume is kept fast, so
  older versions cost only their snapshots' storage.
- Each run's machine has its own copy, so disks cost for as long as their
  machine runs, and a version uses its snapshot's storage until retired.
- A machine image without udev rules naming EBS disks by device (Ubuntu's,
  say) can't have volumes; Amazon Linux, which ECS-optimized images are, can.
- This hasn't yet run against AWS itself: EC2's own API model checks every
  request, and the tests use a fake EC2. The dev cluster's end-to-end checks
  cover the Kubernetes path.
