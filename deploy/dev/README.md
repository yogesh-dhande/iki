# Local development cluster

Iki on a laptop: a kind cluster driven by Tilt, deployed with the production Helm chart.

```console
make dev-up      # create the cluster (once) and start Tilt
make dev-down    # delete the cluster
```

What it needs, what runs and where, signing in, trying a plugin you're writing, and the Slurm
cluster in a container are in
[Run Iki on a laptop with kind](https://ikisci.org/guides/run-iki-on-a-laptop/). Its source is
[`docs/site/src/content/guides/run-iki-on-a-laptop.mdx`](../../docs/site/src/content/guides/run-iki-on-a-laptop.mdx):
change it with what it describes.
