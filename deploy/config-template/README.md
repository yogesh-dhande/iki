# An Iki deployment's config repository

A starting point for the repository that says what each of your Iki
deployments runs: its plugins, their settings, its Helm values and its
image. Copy this directory into a new repository. Everything that changes a
deployment goes through a pull request, so the history of this repository
is the history of what ran where. Each change has a reviewer, a commit and a
signed image. See [ADR 0034](../../docs/adr/0034-deployment-lockfile-and-plugin-builds.md).

```text
deployments/
  staging/
    iki.yaml            # the Iki release: version, server and worker images, chart
    plugins.lock.yaml   # the plugins, each pinned to a commit
    compute.yaml        # the machines actions run on (once a plugin has such actions)
    settings/dna.yaml   # this deployment's values for each plugin's settings
    values.yaml         # Helm values
    image.yaml          # the images CI built (written by CI)
  production/           # the same, for production
plugins.Dockerfile      # Iki's server image plus the built plugins
worker.Dockerfile       # Iki's worker image plus the plugins and their environments
.github/workflows/build.yml
```

## How a change reaches a deployment

1. **Change it in a pull request.** For example, pin a plugin's new commit
   in `deployments/staging/plugins.lock.yaml`. CI runs `iki deploy build`,
   which fetches every plugin at its commit and checks them together: their
   manifests, this deployment's settings for them, and that their migrations
   are complete. A failure is reported on the pull request.
2. **Merge it.** CI builds the deployment's two images, after checking
   Iki's signatures on the images they start from: the server image with
   the plugins, and the worker image with the plugins and the environments
   their actions run in, built from the packages their lockfiles pin. It
   signs both and attests their provenance. Then it opens a pull request
   that points `image.yaml` at the new images, by digest.
3. **Merge that pull request to deploy.** Argo CD or Flux applies the Iki
   chart with `values.yaml` and `image.yaml`. The chart's migrate Job runs
   the plugins' migrations before any new pod starts. Workers lease only
   runs launched with the plugin code they have, so old and new workers
   never run each other's runs during a rollout.

**Promote** a change from staging to production by copying the lockfile
lines, or the `iki.yaml` change for an Iki upgrade, in a pull request.

**Check the target's records before promoting a plugin.** From the
proposed configuration, build the pinned plugins. Sign in to the target
deployment as an Org Admin with access to all projects and run the check
against that deployment. For example, to review `iki/dna` for production,
use the production URL and the manifest from the proposed production build.

```sh
iki deploy build deployments/production --output build/plugins
iki plugin preflight build/plugins/iki_plugin_iki__dna/manifest.json --url https://iki.example.org
```

Under **Admin → Plugins**, upload that `manifest.json` on the plugin's
detail page or use the Plugins list for a new installation. Both let you
download the report. Compare the configuration revision with the deployed
lockfile's digest. Each finding gives an affected count and example record
IDs. SQL checks give exact counts for the whole type. For checks that
examined a sample, review the number of records checked. Findings from
partial samples give lower bounds. Pass `--json` to
the CLI to save the report for the review.

The migrate Job repeats the check before it writes. Fix blocking findings
or, if the review accepts them or the sample's incomplete coverage, put
the report's `candidate_revision` in the target's `values.yaml` in the
promotion pull request. Remove the acknowledgement after the rollout.

```yaml
migrate:
  acknowledgePreflight:
    - "<candidate_revision>"
```

An acknowledgement applies to that candidate manifest. Database
constraints and the checks on reversing migrations still apply.

**Roll back** by reverting the pull request that changed `image.yaml`. The
migrate Job reverses the plugin migrations the older image doesn't have.
If reversing could lose data entered since, it refuses and changes nothing,
and you restore the database from its backup instead.

## Choosing plugin sources

Pin each first-party plugin to `plugins/<name>` in Iki's repository. For a
lab-specific plugin, pin the reviewed commit from your lab's repository
using the name in its manifest. To adapt a worked recipe, follow the
[example copy and test instructions](../../examples/plugins/README.md).

The default build checks active and detached entries against Iki's example,
template, fixture and live-check directories, including on mirrors named under
`plugins.mirrors`. The config template's staging and production builds use
this default. A separate CI or live-check deployment can pass `--test-only`
to build an official example in place. An entry using `local:` also needs
`--allow-local`.

## Compute

Plugins' actions say what they need; `compute.yaml` says what this
deployment has. Each compute template is a kind of machine: Iki's backend
worker pods, Kubernetes Jobs or pools, or EC2 instances, with their size,
placement and price. Each binding says which templates a plugin's
environment runs on, cheapest first:

```yaml
apiVersion: iki/v1
kind: ComputeTemplate
name: backend-small
provisioner: backend_pool
capacity: { cpu: 1, memory: 2Gi }
kubernetes: { runtime_class: gvisor }
---
apiVersion: iki/v1
kind: EnvironmentBinding
environment: iki/protein.protein-tools
templates: [{ template: backend-small }]
```

`iki deploy build` checks every binding against the plugins the lockfile
pins: each template is big enough for the environment, each action fits one
of them, and each data volume has a way to reach the machines. It warns
about actions this deployment can't run. Without `compute.yaml`, only
self-hosted actions run.

A binding of a self-hosted environment names the labels this deployment's
machines need for it instead, such as the lab whose instruments its runs
drive. A run goes only to a registered machine with every label its
environment and its binding name (ADR 0112):

```yaml
apiVersion: iki/v1
kind: EnvironmentBinding
environment: chromatography.chromeleon
labels: [lab-3]
```

The full format is in the
[spec](../../spec/README.md#a-deployments-compute). Ask the plugin's owners
and your compute admins to review changes, with `CODEOWNERS` if you use it.

## Setting it up

- Give CI read access to your plugin repositories as the
  `PLUGIN_REPOS_TOKEN` secret. A GitHub App's token or a fine-grained,
  read-only token is best. Plugins can only be fetched over https or ssh.
  Only the step that fetches them has the token, and git gives it only to
  this GitHub, never to another host a lockfile names, so put no credentials
  in a lockfile's URLs (the build refuses them).
- Protect `main`, and require the build to pass and a review, from the
  plugin's owners for its settings files if you use `CODEOWNERS`.
- In each cluster, admit only images signed by this repository's workflow
  and Iki's (for example with Sigstore's policy-controller or Kyverno). For
  the plugins' images that container-tier runs use, the build approves each
  by signing it into `ghcr.io/<owner>/iki-<deployment>-approvals`, and
  `containerTier.imagePolicy` in the chart admits only those in the runs'
  namespace, with policy-controller
  ([ADR 0063](../../docs/adr/0063-the-cluster-admits-only-images-iki-signed-or-the-deployment-approved.md)).
- Point Argo CD or Flux at each deployment: the Iki chart at the version in
  `iki.yaml`, with `values.yaml` and `image.yaml` as value files.
- Once Iki is up, a Compute admin makes a backend-tier registration token
  in Compute settings (or an operator runs `iki-server
create_registration_token --tier backend --label ...` in a web pod). Store it in the `iki-worker-registration` Secret
  (key `token`, ideally through your secrets manager) and turn on
  `backendWorkers` in `values.yaml`. The worker pods hold only that token:
  no database credentials, no cloud role, and a network policy that lets
  them reach just the server and object storage. Say where those are: your
  load balancer's own addresses (or your ingress controller's pods) in
  `networkPolicy.egress.server.to`, and your object storage's in
  `networkPolicy.egress.objectStorage.to`. The chart won't render workers
  or container-tier runs without them, or with any address block wider
  than a /8 (a /32 for IPv6)
  ([ADR 0155](../../docs/adr/0155-workers-and-runs-reach-only-the-server-storage-and-the-proxy.md)).
  Allow more destinations that actions need in
  `networkPolicy.egress.workers`.
- Bundles move validated entities, with their history, from staging to
  production ([ADR 0096](../../docs/adr/0096-bundles-carry-entities-between-deployments-with-their-history.md)).
  In each deployment, run `iki-server bundle_key` in a web pod, store what it
  prints in the `iki-bundle-key` Secret (key `key`, ideally through your
  secrets manager), and keep the public key it reports. Production imports
  only bundles signed by a key it trusts: put staging's under
  `bundles.trusted` in production's `values.yaml`, where it gets a review.
  `iki-server verify_bundle FILE` checks a bundle's bytes and signature,
  and says whether this deployment trusts it.
- Plugins' actions declare the secrets they need (an API key, a LIMS token).
  Keep them in your secret manager, sync them into one Kubernetes Secret
  (External Secrets, or the Vault or AWS Secrets Manager CSI driver) with a
  key per secret named for its plugin's label (`iki__dna.NCBI_API_KEY` for
  `iki/dna`'s `NCBI_API_KEY`), and set
  `runSecrets.existingSecret`. Only web pods mount it; each run gets only
  the secrets its action declares. Rotate them in the secret manager: the
  next run gets the new value.
- Actions that fetch from the internet (NCBI, UniProt, the PDB) need the
  egress proxy: turn on `egressProxy` in `values.yaml`. Runs then reach the
  hosts their action declares, and only those, through it; each plugin's
  `network` lists them, so review it when you add a plugin. If all traffic
  must pass your corporate proxy, set `egressProxy.upstreamProxy` and point
  `networkPolicy.egress.egressProxy` at that proxy.
- Runs that need machines the cluster doesn't have (eight GPUs, a terabyte
  of memory) can get EC2 instances of their own, started for each run and
  terminated after it: turn on `vmTier` in `values.yaml`, and add
  `aws_ec2` templates to `compute.yaml`
  ([ADR 0074](../../docs/adr/0074-vm-tier-runs-get-cloud-machines-of-their-own.md)).
  Give the provisioner an IAM role (`vmTier.serviceAccountAnnotations`)
  with the policy in that ADR, which lets it start and end only machines
  tagged with `vmTier.owner`. The machines' subnets need a way to the
  server (`vmTier.serverUrl`), object storage and the egress proxy
  (`vmTier.egressProxyUrl`), and nothing else.
  If the server's certificate there is your own CA's, put the CA's
  certificates in a Secret and name it in `vmTier.caSecret`
  ([ADR 0160](../../docs/adr/0160-vm-tier-machines-trust-the-deployments-ca-and-keep-to-what-compute-engine-allows.md)).
- Environments with reference data (AlphaFold's databases, say) fill it
  with a setup build, which a Compute admin asks for under Compute →
  Volumes, and another approves if the environment asks for that
  ([ADR 0075](../../docs/adr/0075-environment-setup-builds-and-managed-volumes.md)).
  On Kubernetes, the cluster needs a CSI driver with snapshots (the EBS CSI
  driver and the snapshot controller on EKS), and each template the
  environment is bound to names a `storage_class` and `snapshot_class` for
  its volumes in `compute.yaml`.
- If your package index is a mirror, pass it to the worker image's build as
  a secret (see `worker.Dockerfile`).
- If CI can't reach github.com, mirror Iki's repository inside your network
  and name the mirror under `plugins.mirrors` in each deployment's
  `values.yaml` ([ADR 0114](../../docs/adr/0114-ikis-own-plugins-can-come-from-a-mirror.md)).
  Only those who can approve `values.yaml` should be able to change it, with
  `CODEOWNERS` and branch protection. The lockfile still names Iki's
  repository, so the same lockfile works anywhere. The build fetches Iki's
  plugins from the mirrors alone, in order, and takes one only at the commit
  the lockfile pins and with a tree Iki's repository has had, as the
  `iki-cli` it runs knows them: a mirror can't serve a commit of its own.
  The build records the mirror each came from, the Plugins page shows it,
  and the deploy audits it. The server refuses plugins from a mirror its
  `values.yaml` doesn't name. Don't send Iki's repository elsewhere with
  git's `insteadOf`: the build refuses that, since it couldn't say where a
  plugin came from.

## Upgrading Iki

Change `version`, `serverImage` and `workerImage` (by digest) and `chart`
in `iki.yaml` together, in staging first. The plugins are rebuilt by that version's
`iki`. The server refuses plugins any other version built.
