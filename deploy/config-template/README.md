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

**Roll back** by reverting the pull request that changed `image.yaml`. The
migrate Job reverses the plugin migrations the older image doesn't have.
If reversing could lose data entered since, it refuses and changes nothing,
and you restore the database from its backup instead.

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
environment: protein.protein-tools
templates: [{ template: backend-small }]
```

`iki deploy build` checks every binding against the plugins the lockfile
pins: each template is big enough for the environment, each action fits one
of them, and each data volume has a way to reach the machines. It warns
about actions this deployment can't run. Without `compute.yaml`, only
client-tier actions run. The full format is in the
[spec](../../spec/README.md#a-deployments-compute). Ask the plugin's owners
and your compute admins to review changes, with `CODEOWNERS` if you use it.

## Setting it up

- Give CI read access to your plugin repositories as the
  `PLUGIN_REPOS_TOKEN` secret. A GitHub App's token or a fine-grained,
  read-only token is best. Plugins can only be fetched over https or ssh.
- Protect `main`, and require the build to pass and a review, from the
  plugin's owners for its settings files if you use `CODEOWNERS`.
- In each cluster, admit only images signed by this repository's workflow
  (for example with Sigstore's policy-controller or Kyverno), and Iki's.
- Point Argo CD or Flux at each deployment: the Iki chart at the version in
  `iki.yaml`, with `values.yaml` and `image.yaml` as value files.
- Once Iki is up, a Compute admin makes a backend-tier registration token
  in Compute settings (or an operator runs `iki-server
create_registration_token --tier backend --label ...` in a web pod). Store it in the `iki-worker-registration` Secret
  (key `token`, ideally through your secrets manager) and turn on
  `backendWorkers` in `values.yaml`. The worker pods hold only that token:
  no database credentials, no cloud role, and a network policy that lets
  them reach just the server and object storage. Allow more destinations
  that actions need in `networkPolicy.egress.workers`.
- If your package index is a mirror, pass it to the worker image's build as
  a secret (see `worker.Dockerfile`).

## Upgrading Iki

Change `version`, `serverImage` and `workerImage` (by digest) and `chart`
in `iki.yaml` together, in staging first. The plugins are rebuilt by that version's
`iki`. The server refuses plugins any other version built.
