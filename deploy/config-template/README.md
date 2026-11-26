# An Iki deployment's config repository

A starting point for the repository that says what each of your Iki deployments runs: its
plugins, their settings, its Helm values and its image. Copy this directory into a new
repository. Everything that changes a deployment then goes through a pull request.

```text
deployments/<name>/     # iki.yaml, plugins.lock.yaml, compute.yaml, settings/, values.yaml, image.yaml
plugins.Dockerfile      # Iki's server image plus the built plugins
worker.Dockerfile       # Iki's worker image plus the plugins and their environments
.github/workflows/build.yml
```

How a change reaches a deployment, promoting and rolling back, and setting up CI, signed images,
workers, bundles, secrets, the egress proxy, the VM tier, volumes and mirrors are in
[Keep a deployment's configuration in a repository](https://ikisci.org/guides/keep-configuration-in-a-repository/).
The lockfile's and `compute.yaml`'s format is in
[Configure a deployment's plugins and compute](https://ikisci.org/guides/configure-plugins-and-compute/).
