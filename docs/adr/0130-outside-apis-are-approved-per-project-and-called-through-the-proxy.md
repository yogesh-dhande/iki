# 0130. Outside APIs are approved per project, called through the egress proxy, and recorded call by call

- Status: Accepted
- Date: 2026-11-18
- Refines: [0053](0053-runs-reach-the-internet-through-an-egress-proxy.md),
  [0054](0054-run-secrets.md),
  [0050](0050-agents-runs-on-costly-tiers-wait-for-approval.md),
  [0070](0070-runs-show-what-they-cost.md),
  [0071](0071-budgets-warn-and-can-stop-spending.md),
  [0043](0043-workers-lease-jobs.md),
  [0047](0047-workers-run-only-the-code-a-run-was-launched-with.md)

## Context

Some models are only available behind a company's API: hosted structure
prediction, large language models, commercial ADMET predictors. A run that
uses one sends the lab's data to a third party, spends the organization's
money with the organization's API key, and gets an answer from a model that
can change under it without notice. Pharma companies expect four things of
that:

- **Someone accountable approved the destination.** Data goes only to
  processors the organization agreed to, for a stated purpose. GitHub's
  allowed actions and its OAuth app approval are the precedent: an
  organization's owners allow each third party, and it's refused until they
  do.
- **The person sending it knows what goes.** Before the data leaves, they're
  told which data, and to whom.
- **The key isn't the plugin's.** An API key is the organization's, rotated
  in its secret manager. Plugin code shouldn't hold it, and it should only
  ever reach the provider it's for.
- **The record says what answered.** GxP reproducibility claims rest on
  knowing which model, at which version, with which parameters, gave which
  answer. An outside model has no commit to pin, so the record must keep
  what the provider said at the time.

Until now an action could reach a host by naming it under `network`
(ADR 0053) and get a key as a secret (ADR 0054). That gives the command the
key, records hosts and bytes but nothing of what was asked or answered, and
nobody but the plugin's reviewer agreed to the destination. Many of these
APIs are also asynchronous: submit a job, then poll for hours. A worker
waiting for it would hold a machine doing nothing.

## Decision

### A plugin declares its outside APIs

`plugin.yaml` names each API under `apis`, and each action that calls one
says what it sends:

```yaml
# plugin.yaml
apis:
  foldcloud:
    label: FoldCloud
    host: api.foldcloud.example # or {settings.<name>} (ADR 0083)
    purpose: Predicts protein structures with FoldCloud's hosted models.
    credential:
      secret: FOLDCLOUD_API_KEY # Authorization: Bearer <key> unless header/prefix say
    reports:
      model: /model # a JSON pointer in the answer, or header:<name>
      version: /model_version
      parameters: [/model, /num_recycles] # JSON pointers in the request
      usage: { gpu_seconds: /usage/gpu_seconds }
    jobs: # submit, then poll
      poll: /v1/predictions/{id}
      status: /status
      done: [succeeded]
      failed: [failed, canceled]
      message: /error
      cancel: /v1/predictions/{id}/cancel
      every: 30s
      timeout: 1d
      webhook: { signature: standard-webhooks, secret: FOLDCLOUD_WEBHOOK_SECRET }

# actions/predict_structure.yaml
apis:
  foldcloud:
    sends:
      inputs: { protein: [sequence] }
      params: [model]
```

The plugin's checks refuse an API on the self-hosted tier (Iki can't keep the
key from the machine's owner), a host that's also under `network`, a
credential that's also an action's secret, a `sends` naming a field its input
doesn't have, and a batchable action whose API parks (one job runs several
runs). Hosts are exact names: an API is one service, not a domain.

### Org Admins approve each destination, per project

An API can't be used in a project until an Org Admin approves it there:
**Settings → Outside APIs** lists every installed API, its host and purpose,
the actions that call it and what each sends, the credential's state, and
each project's approval. Approving records who, when and the host and
purpose approved; an approval stops counting once the plugin's API names
another host or purpose, which must be approved again, as GitHub asks owners
again when an app wants more. Approving is web-only (`web_only` for every
token), so no agent can approve where its runs send data. Revoking works from
the API too. Both are audited (`outside_api.approved`,
`outside_api.revoked`).

A launch without the approval is refused (409 `outside_api_not_approved`)
with what to do: "FoldCloud (api.foldcloud.example) isn't approved for
Antibody discovery. An Org Admin approves it in Settings → Outside APIs: Ada
Lovelace or Grace Hopper." The egress proxy checks the approval again for
every call, so revoking stops a running attempt's next call, and parked runs
end.

### People consent to what's sent before launching

An action's catalog entry carries each API's consent statement: "This sends
your data to FoldCloud (api.foldcloud.example): each Protein's sequence and
the model param. Predicts protein structures with FoldCloud's hosted
models." The web app's launch form shows it with a box to tick, and launching
needs it: `consent: ["acme/hosted_models.foldcloud"]` in the request, or 409
`consent_required` with the statements. The SDKs take `consent=` and raise
with the statements. Each launch's consent is kept, append-only, with the
statement as shown, who gave it and how (including the agent, ADR 0072), and
every run, batch and workflow points to it. A workflow's launch consents for
its steps; reproducing a run consents again.

**Agents don't consent for people.** Through MCP, and from triggers, a
launch whose action sends data outside waits for the person's approval, as
costly tiers do (ADR 0050). The approval page shows the statement, and
approving is the person's consent, recorded as theirs with the agent beside
it. MCP's `run_action` says so outside the data fence.

### The proxy calls the API, with the organization's key

The command calls the API at its real address, `https://api.foldcloud.example`
(also given as `IKI_API_FOLDCLOUD_URL`), through `HTTPS_PROXY`, without a
key. For a host that's an API of the attempt's action, the egress proxy
doesn't tunnel: it ends the TLS connection itself, reads each HTTP/1.1
request, checks the project's approval, adds the credential header from the
run secrets (`acme__hosted_models.FOLDCLOUD_API_KEY`, read at each call, so
rotation is immediate), and makes the call over TLS it verifies. The key
never enters the worker's machine, and it can only ever be sent to its API's
host.

So that the command trusts the proxy for those hosts, and nothing else:

- **Each attempt gets its own certificate authority.** At lease, the server
  makes a CA for that attempt alone, signs a certificate for each of its
  APIs' hosts with the proxy's public key, and throws the CA's private key
  away. The CA can never sign anything again. The certificates are kept with
  the attempt, and expire with its timeout.
- **The worker trusts it for that command.** It writes the system's roots
  plus that CA to a file and points `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and
  `CURL_CA_BUNDLE` at it, and `NODE_EXTRA_CA_CERTS` at the CA alone, which
  Node adds to its own roots.
- **The proxy's key is stable.** It comes from a Secret
  (`egressProxy.gatewayKey`), so a restarted replica can still serve the
  attempts that were leased before. Each replica registers its public key at
  start; leases sign for the keys seen in the last ten minutes.

What it costs:

- The proxy reads API calls: for these hosts it's an endpoint, not a tunnel.
  It keeps hashes and the declared fields, never bodies, and its pod now
  mounts the run secrets as the web pods do.
- Commands must use the system's trust settings. A client that bundles its
  own roots and ignores those variables, or pins certificates, can't call an
  API this way. Python's `requests`, `httpx` and `urllib`, `curl`, Node and
  the providers' SDKs built on them do.
- Calls are HTTP/1.1. The proxy offers only `http/1.1`, so HTTP/2 clients
  fall back; gRPC and WebSockets aren't supported. Streamed answers
  (server-sent events) pass through as they come.
- TLS is done twice, and the proxy asks for answers uncompressed, so that it
  hashes what the API sent and reads the declared fields.

### Each call is recorded, for provenance and cost

For every call (and every poll and cancel) Iki keeps, with the run: the API
and host, method and path (never the query, which may hold data), status,
time, bytes each way, the SHA-256 of the request and of the response body as
the API sent it, the model and version the answer reported, the request's
declared parameters, the declared usage, and what it cost. They're recorded
by the proxy, not by the plugin's code, so a command can't misstate them. The
run's page shows them, as do `GET /runs/{ref}/api-calls`, MCP's `get_run`,
both SDKs and bundles. Reproducing such a run calls the API again, after
consenting again: the provider may answer differently, and each run's record
says what it answered then.

### Calls cost, and count against budgets

Prices are the deployment's, beside its templates' (ADR 0070): the
compute config gives each API a price per call and per unit of usage it
reports, in the deployment's currency. `unpriced-api` warns about one without
any. Each priced call becomes a cost record (`kind: api`) of its run, project,
team and action, so reports, the finance CSV and budgets count it.

A bundle preserves the amount and currency Iki recorded for the original call.
The importing deployment displays that evidence without counting it against
its budgets or creating a cost record.

Actions that use a priced API are costly as container and VM runs are: an enforced
budget that's spent refuses their launches and holds their queued runs, on
any tier, and an agent's runs of them wait for approval.

### A run waiting on a long job parks

An API with `jobs` lets a command submit, then **park**: its output is
`{"park": {"api": "foldcloud", "job": "<the provider's job ID>", "state":
{...}}}`. The attempt ends `parked`, its worker is free, and the run is
`parked`, "Waiting on FoldCloud". The egress proxy's poller polls the job's
`poll` path every `every`, with the same credential injection and recording.
When its `status` is one of `done`, the run gets a new attempt, pinned to
the code it was launched with (ADR 0047), whose input carries `resume`: the
job, the saved state and the final answer. When it's `failed`, the run fails
with the provider's message.

- **Webhooks wake it.** Each attempt gets a callback URL per API
  (`IKI_API_FOLDCLOUD_CALLBACK_URL`) to give the provider. Its last part is an
  HMAC of the job and API under the deployment's secret key, so Iki stores
  nothing for it and a guessed URL is refused. A callback must also be
  signed (Standard Webhooks, or HMAC-SHA256 of the body in a header) with the
  API's webhook secret; one that verifies makes the poller poll now. Its body
  is never trusted: what the run resumes with is what the poll answered.
- **Timeouts and cancelling** end the run at once: a park that outlives
  `timeout` (at most 7 days) ends it `timed_out`, and cancelling a parked run
  ends it `cancelled`. The poller then asks the provider to cancel the job,
  where the API says how (`cancel`), and records how that went. Asking sends
  only the job's ID, so it's done even once the approval is revoked.
- **Parked runs hold nothing.** They don't count against running or waiting
  quotas, and they're never retried: a resume isn't an attempt that failed.
- **Each attempt keeps its output.** The parked attempt's says what it
  waited on, and the resuming attempt's has the results, each as its worker
  sent it (ADR 0044). The run's output, for its metrics, messages and
  reproductions, is its last attempt's; a bundle carries each on its attempt.
- The poller runs in the egress proxy, because it holds the keys and is the
  only thing that reaches the internet. Its replicas take due parks with
  `FOR UPDATE SKIP LOCKED`.

### The example

The original implementation shipped the fictional FoldCloud provider as a
first-party plugin. Its current home is the
[hosted-model author example](../../examples/plugins/hosted_models/README.md),
under `acme/hosted_models` ([ADR 0218](0218-first-party-plugins-and-author-examples.md)).
The test deployment supplies the fake host and test credentials. The action
submits a job, parks, then imports the result with its model provenance.
The tests exercise project approval, launch consent, proxy calls, callback,
polling, cancellation, resume and billing.

## Consequences

- Positive: data reaches a third party only where an Org Admin approved it,
  after the person sending it saw what goes, and the record says who did
  each.
- Positive: plugin code never holds the organization's key, and the key can
  only reach its provider.
- Positive: an outside model's answer has the provenance an outside model can
  have: what it said it was, with what it was asked, and a hash of what it
  said, recorded by Iki.
- Positive: hours-long API jobs cost no worker time, and webhooks make them
  finish as soon as they're done.
- Negative: the proxy becomes the endpoint of API traffic, and holds the run
  secrets. It's a more privileged pod than a tunnel was.
- Negative: clients that pin certificates or ignore the system's trust
  settings, HTTP/2-only APIs, gRPC and WebSockets can't be called this way.
- Negative: approving per project is more work for Org Admins than
  approving once. It's deliberate: a project's data and purpose decide where
  it may go.

## Alternatives considered

- **Give the command the key, as a secret:** the key could reach any host the
  action declares, and nothing records what was asked or answered.
- **A plain-HTTP hop to the proxy:** the command would send the lab's data
  unencrypted across the cluster's network, and from VMs across their VPC.
- **One certificate authority for the deployment:** a leaked key could
  intercept every run's connections. A CA per attempt, thrown away once it
  has signed, can't sign anything again.
- **A worker waiting on the job:** a GPU-tier machine idle for hours, and a
  run that fails when the machine goes away.
- **Trusting the webhook's body:** anyone who learns the URL and secret could
  feed a run a result; polling means the answer always comes from the API.
