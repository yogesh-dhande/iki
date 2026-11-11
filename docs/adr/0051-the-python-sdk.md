# 0051. The Python SDK signs in as the person and keeps the API's shapes

- Status: Accepted
- Date: 2026-11-11

## Context

Scientists work in notebooks and scripts, and the plan has them use Iki
there through a Python SDK. They need to search, read and change entities,
run actions and wait for them, download files, and get DataFrames. What they
do must be on the record as theirs (ADR 0028). The machines they work on vary:
laptops with a keychain, shared servers and HPC clusters without one, and
corporate networks with proxies that inspect TLS. Regulated labs expect
sign-ins that can be ended and audited, and changes that say why.

## Decision

- **A package of its own.** The SDK is `ikisci` on PyPI, imported as `iki`,
  at `sdk/python`. It depends on `httpx2` and `keyring`; pandas is an
  optional extra (`ikisci[pandas]`). It doesn't depend on the server, so
  installing it pulls in nothing else of Iki's.
- **The API's shapes, typed.** Answers are the API's JSON, as plain dicts
  and lists, not classes of their own. `iki.schema` has a `TypedDict` for
  each, generated from the OpenAPI document (`make client`, and a test fails
  when they're stale). Editors complete keys and type checkers check them,
  and the SDK can't drift from the API.
- **Signing in is the device flow.** `iki.login()` uses the first-party
  public client `iki-cli`, which the CLI shares, and asks for `read`, `write`
  and `run`. The person signs in with their organization's SSO in the
  browser, chooses scopes and projects there (ADR 0028), and can end the
  sign-in under **Account → Apps** or with `iki.logout()`, which revokes
  both tokens. The SDK never sees a password. It opens the browser only
  when one is in front of the person: not over SSH, and not on Linux without
  a display.
- **Kept in the keychain, else a private file.** Tokens go in the system
  keychain (macOS Keychain, Windows Credential Manager, the Secret Service),
  under the deployment's URL. Where there's no keychain, they go in
  `~/.config/iki/credentials.json`, made 0600 in a 0700 folder.
  `IKI_URL` and `IKI_TOKEN` (a service account's token, say) work instead
  for pipelines, and then nothing is stored.
- **Renewing is locked across processes.** Iki rotates refresh tokens and
  ends the whole sign-in when one is used twice. So renewing takes a file
  lock, and a process that finds the stored tokens renewed since it read them
  uses those. Ten notebooks on one machine share one sign-in safely.
- **HTTPS only, with the system's certificates.** Plain HTTP is refused
  except on this machine (`localhost`, loopback, `*.localhost`). Certificates
  are checked against the operating system's trust store (`truststore`),
  so corporate proxies that inspect TLS work without turning checks off.
- **Changes say which revision they're of.** `update`, `archive` and
  `restore` take the revision read and send it as `If-Match`. A change to a
  revision someone has since changed raises `iki.Conflict`, and nothing
  changes (ADR 0035). They also take a reason, which the revision records.
- **Polite to the server.** Answers that say to wait (429, and 503 for
  reads) are retried after `Retry-After`, up to a minute and four times. An
  expired access token is renewed once.
- **Files skip Iki's token.** A download follows Iki's short-lived signed
  URL without the Authorization header, which is for Iki alone. It's
  written beside the target and moved into place only when its SHA-256
  matches what Iki recorded. Only the last part of the file's name is
  used, so a name can't put it outside the chosen folder.
- **Errors say why.** A refusal raises an `iki.ApiError` subclass (`Invalid`,
  `NotFound`, `Forbidden`, `Conflict`, `RateLimited`) carrying the problem
  document's detail, code, field errors and request ID.

## Consequences

- Notebooks work on shared clusters and behind TLS-inspecting proxies with
  no setup beyond `pip install` and `iki.login()`.
- Everything done through the SDK is recorded as the person's, `via` the API,
  through "Iki CLI and Python SDK", in revisions and the audit log.
- Results are dicts, not objects with methods; `iki.Client` holds the
  methods. New API fields appear in the SDK when `make client` runs, with no
  hand-written models to update.
- The CLI and SDK share an OAuth client, so **Account → Apps** shows them as
  one app.
