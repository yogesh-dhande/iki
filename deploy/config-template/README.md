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
    iki.yaml            # the Iki release: version, server image, chart
    plugins.lock.yaml   # the plugins, each pinned to a commit
    settings/dna.yaml   # this deployment's values for each plugin's settings
    values.yaml         # Helm values
    image.yaml          # the image CI built (written by CI)
  production/           # the same, for production
plugins.Dockerfile      # Iki's server image plus the built plugins
.github/workflows/build.yml
```

## How a change reaches a deployment

1. **Change it in a pull request.** For example, pin a plugin's new commit
   in `deployments/staging/plugins.lock.yaml`. CI runs `iki deploy build`,
   which fetches every plugin at its commit and checks them together: their
   manifests, this deployment's settings for them, and that their migrations
   are complete. A failure is reported on the pull request.
2. **Merge it.** CI builds the deployment's image from Iki's server image
   (whose signature it checks first) and the plugins. It signs the image and
   attests its provenance. Then it opens a pull request that points
   `image.yaml` at the new image, by digest.
3. **Merge that pull request to deploy.** Argo CD or Flux applies the Iki
   chart with `values.yaml` and `image.yaml`. The chart's migrate Job runs
   the plugins' migrations before any new pod starts.

**Promote** a change from staging to production by copying the lockfile
lines, or the `iki.yaml` change for an Iki upgrade, in a pull request.

**Roll back** by reverting the pull request that changed `image.yaml`. The
migrate Job reverses the plugin migrations the older image doesn't have.
If reversing could lose data entered since, it refuses and changes nothing,
and you restore the database from its backup instead.

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

## Upgrading Iki

Change `version`, `serverImage` (by digest) and `chart` in `iki.yaml`
together, in staging first. The plugins are rebuilt by that version's
`iki`. The server refuses plugins any other version built.
