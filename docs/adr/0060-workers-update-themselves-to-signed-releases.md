# 0060. Workers on people's computers update themselves to signed releases

- Status: Accepted
- Date: 2026-11-12
- Refines: [0058](0058-workers-on-peoples-computers-sign-in-as-them.md),
  [0017](0017-supply-chain-security.md)

## Context

A deployment's workers come in images, so an upgrade rolls them out. A
worker on someone's laptop stays on whatever they installed. Over time a
lab ends up with a spread of versions, some too old for the server's
protocol, and nobody asks IT to reinstall anything until it breaks.

Updating a binary that runs other people's code is a supply-chain risk.
Pharma IT wants to know that what runs came from Iki's own release
process, and many sites can't reach GitHub at all.

## Decision

**The server says which release to run.** `IKI_WORKER_VERSION` defaults to
the server's own version. A deployment can pin another, or turn updates off
with `IKI_WORKER_UPDATES`. A client-tier worker that runs anything else is
told the release, and where to download it, in `GET` and `PUT
/worker/v1/me`. Workers in images never are, since they come with their
deployment. Development versions are never offered.

**One minor release back is accepted; older gets no jobs.** The server
takes the release it wants, the minor release before it, and anything
newer. A worker older than that is refused at lease with 409
`worker_outdated`, including the release to update to when it can update
itself. A fleet can then move one release at a time, and a worker that
can't update says why and stops, rather than failing jobs.

**Downloads can come from a mirror.** `IKI_WORKER_DOWNLOADS` is GitHub's
releases by default. An internal mirror laid out the same way
(`v<version>/iki-worker-<version>-<target>.tar.gz` and its
`.sigstore.json`) works the same.

**The worker checks the signature itself, offline.** Each release tarball
is signed keylessly by the release workflow, and its Sigstore bundle is
published beside it. Before installing, the worker checks that bundle
against Sigstore's public-good trusted root, which it embeds. It uses
`sigstore-verify`, the Sigstore project's Rust library. The signing
certificate must name the release workflow of the repository the worker
was built from, at that version's tag, issued by GitHub Actions. So it
needs no network beyond the download, and the mirror needn't be trusted. A
worker built from source has no repository to check against, and never
updates itself.

**It updates only while idle, and only if the new one runs.** The worker
checks when it starts and every ten minutes while it holds no job. It
unpacks the new `iki-worker` beside itself and runs `--version` to check it
is the release it should be. Only then does it rename the new binary over
itself and start it in its own place, with the same arguments and saved
token. If any step fails, it logs why and carries on as it was. It tries
each version once. `IKI_WORKER_AUTO_UPDATE=0` leaves updates to whoever
installed it, and the worker says when one is wanted.

**`iki-worker verify` checks a release by hand.** IT can verify a download
the same way the worker does before rolling it out. The release workflow
runs it against every tarball it signs, and against a tampered copy and
another repository's identity, which it must refuse. Every release
therefore proves the updater accepts exactly its own signatures.

## Consequences

- Laptop workers keep up with the server without anyone reinstalling
  them, and every update is to code signed by Iki's release workflow.
- A worker installed somewhere its user can't write (by IT, under
  `/usr/local`) can't replace itself. It says so, and keeps running while
  it's within one minor release.
- The embedded trusted root ages. Each release brings the current one, and
  Sigstore's root keys change rarely and with long overlap.
- Building the worker needs a C compiler, for the cryptography
  `sigstore-verify` uses (aws-lc). The release and image builds have one.

## Alternatives considered

- **Leaving updates to IT.** This remains available
  (`IKI_WORKER_AUTO_UPDATE=0`, or turning updates off on the server). As
  the default, though, it leaves most laptops behind.
- **Trusting the download over HTTPS.** That trusts the mirror, and
  whoever controls it.
- **Fetching Sigstore's root over TUF at update time.** That needs network
  access many sites don't allow, for little gain over the root each release
  embeds.
