# 0059. Workers on people's computers fetch plugin code and build its environments

- Status: Accepted
- Date: 2026-11-12
- Refines: [0047](0047-workers-run-only-the-code-a-run-was-launched-with.md),
  [0058](0058-workers-on-peoples-computers-sign-in-as-them.md)

## Context

A worker runs only the plugin code a run was launched with, and only in
environments it has built with the digest the server expects
([ADR 0047](0047-workers-run-only-the-code-a-run-was-launched-with.md)).
A deployment's workers get both from their image, which `iki deploy build
--workers` prepares. A worker on someone's laptop has no image. Asking
people to export each plugin at the right commit, and to build each
environment by hand, would mean nothing ever matches. Every upgrade would
also stop their worker until they did it again.

## Decision

**Every deployment build bundles each pinned plugin's code.** `iki deploy
build` writes each plugin it fetched from a commit as a gzipped tar,
`<name>-<sha>.tar.gz`, beside the server's plugin manifests. The tar holds
only regular files, with no times or owners, so the same commit always
gives the same bytes. `lock.json` records each bundle's SHA-256 and size.
For each environment workers build, it also records the digest and the
files in the bundle that build it. A plugin with a symbolic link is
refused, since a link could point anywhere on the computer that unpacks it.

**Workers fetch what the server installs.** `GET /worker/v1/plugins`
lists the bundles. `GET /worker/v1/plugins/{name}/{sha}` downloads one.
Both take a worker's token. A worker with no `IKI_PLUGINS_DIR` fetches
each bundle into its cache and checks the size and SHA-256 before it
unpacks anything. It unpacks only regular files and directories, inside
the plugin's directory, with a cap on files and bytes.

**It builds environments the way the image does.** A Python environment
is made with `uv venv` for its declared version, then `uv pip install
--require-hashes --no-deps` from the bundle's requirements file. A conda
environment is made with `micromamba create` from its lockfile. Before
building, the worker computes the digest from the bundle's files, as
`iki deploy build` does, and refuses an environment whose files don't give
the expected digest. It then reports that digest, so leasing and each
attempt's recorded build work as for any worker. If an environment can't
be built (no uv installed, say), the worker leaves it out and still runs
the plugin's other actions. The warning it logs names what to install.

**What it fetched is kept by commit and digest.** The next start reuses
it. Code and builds the server no longer lists are removed. An idle worker
checks the list every ten minutes. When the list changes, the worker
fetches the new code, builds the new environments, and tells the server
what it runs now. An upgrade reaches it without a restart.

## Consequences

- A laptop worker runs exactly the code and environments a deployment
  worker would, checked the same way, so its runs' provenance is as good.
- People need uv (or micromamba) for plugins with Python (or conda)
  environments. The worker says so when one is missing.
- The server image is larger by the plugins' code, which is small next to
  the environments the worker image holds.
- Plugins from a local directory (development) aren't bundled. Workers
  for those still use `IKI_PLUGINS_DIR`.

## Alternatives considered

- **Fetching from the plugin's git repository.** Laptops would need
  credentials for every plugin repository, and network access to it. The
  server already holds the exact code it installs.
- **Shipping built environments.** These depend on the operating system and
  architecture, and are large. Pinned, hashed requirements give the same
  packages wherever they're built.
- **Signing bundles.** The worker already reaches the server over
  certificate-checked HTTPS, with its own token, and checks each bundle
  against the server's list. Signatures matter for what nothing else vouches
  for, such as the worker binary's own updates.
