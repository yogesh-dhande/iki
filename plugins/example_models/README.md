# example_models

An example of a plugin that calls a model behind an outside API: it predicts
protein structures with FoldCloud, a made-up hosted structure-prediction
service (see `scripts/predict.py`). To call a real model, such as a hosted
structure predictor, a language model or an ADMET service, copy it and
change the API's declaration and the script.

It shows what such a plugin needs
([ADR 0130](../../docs/adr/0130-outside-apis-are-approved-per-project-and-called-through-the-proxy.md)):

- **`apis.foldcloud`** in `plugin.yaml` declares the API: its label, its host
  (the deployment's `fold_api_host` setting), why data goes there, the key
  it needs, where its answers say which model and version answered, and how
  to poll and cancel its jobs. An Org Admin approves it for each project in
  Settings → Outside APIs; until then, runs in that project are refused.
- **`predict_structure`** says what it sends: each protein's sequence and the
  `model` and `num_recycles` params. People see that before they launch it,
  and agree to it; an agent's runs wait for their person to.
- **No key in the plugin.** The script calls `IKI_API_FOLDCLOUD_URL` through
  the egress proxy without one. The proxy adds the organization's key, from
  the run secrets (`iki__example_models.FOLDCLOUD_API_KEY`), and records each
  call: the model and version FoldCloud reported, the parameters, hashes of
  what went each way, and its cost.
- **Parking.** FoldCloud takes a job and answers later, so the script
  submits it and parks (`{"park": ...}` in its output). The run waits without
  holding a worker; Iki polls the job, or FoldCloud calls back, signed, on
  `IKI_API_FOLDCLOUD_CALLBACK_URL`. Once it's done, the script runs again
  with `resume` in its input and imports the predicted structure.

A deployment sets `fold_api_host`, the key and the webhook secret
(`iki__example_models.FOLDCLOUD_WEBHOOK_SECRET`), and prices the calls with an
`ApiPrice` in its compute config. The dev cluster runs it against a fake
FoldCloud (`deploy/dev/manifests/fake-foldcloud.yaml`), and its tests against
a stand-in on localhost (`server/tests/test_example_models.py`).
