# 0041. Each deployment's compute is reviewed config, checked against its plugins

- Status: Accepted
- Date: 2026-11-11
- Refines: [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0040](0040-action-and-environment-manifests.md)

## Context

Environments say what actions need, in no cloud's terms (ADR 0040). Someone
has to say what each deployment has: which instance types, subnets and IAM
profiles, which node pools and runtime classes, what they cost. In pharma,
that is a platform or compute team, often with a change-control process,
and they need to see a change before it spends money or reaches a new
network.

## Decision

**A deployment's compute lives in its config repository**, as
`compute.yaml` beside the lockfile, in two kinds of document:

- A **compute template** (`kind: ComputeTemplate`) is a kind of machine: a
  provisioner (`backend_pool`, `k8s_job`, `k8s_deployment` or `aws_ec2`),
  which fixes its tier; its capacity, given explicitly; where it runs;
  how it gets each data volume; its limits; and its price.
- An **environment binding** (`kind: EnvironmentBinding`) lists the
  templates one plugin environment runs on, each with a priority (spot
  before on-demand, say). For each job, Iki picks among the templates it
  fits: the lowest priority number, then the smallest.

Capacity is written in the template rather than looked up from the
cloud, so the build can check it with no cloud access, and a reviewer sees
the numbers the scheduler uses.

**`iki deploy build` checks it against the plugins it pins**, and fails on:

- a binding to an environment no installed plugin has, or to a template on
  another tier
- a template smaller than its environment's minimum
- an action that fits none of its environment's templates on its tier
- an environment volume with no strategy on a template it's bound to, or a
  volume written by one run at a time that isn't attached exclusively

It warns about actions the deployment can't run, since their environment
isn't bound on their tier, and about templates nothing uses. It writes the
result as `compute.json` in the image, beside the plugins, so a deployment
runs exactly what was reviewed and signed.

Settings that only some provisioners take, such as `aws` and `lifecycle`
for `aws_ec2` and `kubernetes` for the rest, are refused on the others,
rather than ignored.

## Consequences

- Changing compute means a pull request and a new image, like changing a
  plugin. A change to a price or a subnet doesn't wait on anyone else's
  release.
- A deployment without `compute.yaml` runs only client-tier actions. The
  build warns about every action it can't run.
- The scheduler, autoscaler and provisioners that use these come with the
  tiers (commits 23, 31 and 35). Templates for other clouds (GCE first)
  are new provisioners, with their own settings.
