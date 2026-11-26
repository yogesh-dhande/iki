# Worker execution fixture

`make dev-ci` exercises a fresh pod that assigns storage and a persistent pod that tidies existing labels. It checks pod identity and labels, the worker image digest, per-run accounting and reuse of the warm pod.

A second command reads a storage label from a managed volume. Each setup stamps the labels with its own build ID. The check reads them back from a snapshot, a shared directory and a bucket in turn, retiring each version before reading the next. The returned label and the run's recorded setup must both identify the expected build.

From the repository root, run the local checks with a matching `iki-worker` on the path.

```sh
uv run iki plugin test deploy/dev/fixtures/plugins/worker_execution
uv run pytest deploy/dev/tests/test_worker_reference.py
```
