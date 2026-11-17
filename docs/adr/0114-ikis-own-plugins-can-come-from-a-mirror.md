# 0114. Iki's own plugins can come from a mirror, which can't serve commits of its own

- Status: Accepted
- Date: 2026-11-17
- Refines: [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0113](0113-plugins-are-named-in-a-namespace.md)

## Context

ADR 0113 has a lockfile take Iki's own plugins (`iki/...`) only from Iki's
repository on github.com. Many pharmaceutical sites can't reach it. Their
build machines are air-gapped, or may use git only through an internal
mirror that security has reviewed. They need Iki's plugins from that
mirror. Two things must still hold:

- **The same lockfile works anywhere.** It still names Iki's repository.
- **`iki/dna` at a commit means Iki's code.** A copy a lab changed is that
  lab's plugin, in its own namespace.

A commit's hash proves what the commit holds, but not who made it. A
mirror, or a fork posing as one, could hold a commit of its own and serve
it under the hash a lockfile pins.

Other tools solve this the same way:

- **Terraform's `provider_installation`.** A `network_mirror` serves
  providers, and the lockfile's hashes, which came from the registry, check
  what it serves.
- **Go's `GOPROXY`.** A proxy serves modules, and the checksum database
  checks each one. `GONOSUMDB` opts modules out, never by default.
- **git's `insteadOf`.** It sends a URL somewhere else, quietly. Nothing
  records that it did.

Each names the mirror in the operator's configuration, not in the project,
and checks what it serves against hashes from the origin.

## Decision

**The deployment's admin names the mirrors.** They go in its Helm values,
as `plugins.mirrors`, a list of https or ssh URLs:

- **The build.** The config repository's CI passes the list to
  `iki deploy build --mirror`.
- **The server.** It reads the list as `IKI_PLUGIN_MIRRORS`.

Only those who can approve `values.yaml` can change the list, as with
`bundles.trusted` ([ADR 0096](0096-bundles-carry-entities-between-deployments-with-their-history.md)).
The config template says to protect it with `CODEOWNERS` and branch
protection.

**The lockfile is unchanged.** It still names Iki's repository and pins
the commit. With mirrors named, the build fetches Iki's plugins from the
mirrors alone, in order, and never from github.com. A site that requires a
mirror wants no traffic to github.com, as with a Terraform `network_mirror`
and no `direct` block. Other plugins come from the repositories their
lockfile entries name.

**A plugin from a mirror is checked against what Iki's repository has
had.**

- **The commit.** It must be the one the lockfile pins. Git checks every
  object it receives (`fetch.fsckObjects`).
- **The tree.** The plugin's directory at that commit must have a tree Iki's
  repository has had. `iki_spec/first_party_trees.json` lists those trees
  by hash, and travels in the `iki-cli` package the site already installs,
  at the version its deployment runs. It plays the part of Go's checksum
  database or Terraform's lockfile hashes.
- **A lab's changed copy.** Its tree isn't listed, so the build refuses it
  and says to give the plugin the lab's namespace.
- **The same code from another commit.** The tree is listed, so the build
  takes it: the code is Iki's, which is what `iki/` promises.

The list only ever grows. `make first-party` adds each plugin's current
tree, and a test fails while one is missing, so every commit of Iki's
repository lists its own plugins' trees. A commit of Iki's newer than the
site's `iki-cli` needs that newer `iki-cli`, which is the release a
regulated site would take anyway. A fetch from Iki's own repository isn't
checked against the list, since that repository is the origin.

**git's config can't send Iki's repository somewhere quietly.** The build
asks git where its config sends each URL (`git ls-remote --get-url`). For
Iki's plugins, the build refuses an `insteadOf` that points Iki's
repository, or a mirror, at another repository, and says to name a mirror
instead. A rewrite that only adds credentials, to the same repository, is
fine. A lab's own plugins are the lab's to route as its git config says:
their lockfile entries name their repositories, and nothing checks them
against a list.

**Where each commit came from is recorded.**

- **The build.** `lock.json` gives each plugin's `fetched_from`: the
  lockfile's repository, or the mirror. The registry module lists the
  mirrors it used, as `MIRRORS`.
- **The deploy.** It audits `fetched_from` with `plugin.installed`, and with
  `plugin.updated` when the commit changes.
- **The Plugins page.** It shows each plugin's repository and, when they
  differ, where it was fetched from.

Recorded URLs never hold credentials. A mirror's URL may not have any: give
them to git's credential helper, as CI does for plugin repositories.

**The server refuses mirrors its deployment doesn't name.** If the build
fetched from a mirror missing from `IKI_PLUGIN_MIRRORS`, every server
process refuses to start, the migrate Job included, so nothing is
migrated. This holds even if someone changed the CI workflow to pass a
mirror of their own. Mirrors match however they're written: https or ssh,
with or without `.git`.

## Consequences

- An air-gapped or mirror-only site runs Iki's plugins from its own mirror,
  with the lockfile Iki's docs show. Its audit trail says the mirror served
  them.
- A mirror can only serve trees Iki's repository has had. A lab that
  changes one of Iki's plugins names it in its own namespace, as before.
- **The list grows with each change.** Every change to a first-party
  plugin adds a tree hash to `first_party_trees.json`, through
  `make first-party`, which `make check` enforces. That's about forty bytes
  per change.
- A site on a mirror can pin Iki's plugins only at trees its `iki-cli`
  knows. To take a plugin fix before it's released, it must update
  `iki-cli` or reach Iki's repository.
- **The trust root moves to the package.** The list's integrity is the
  `iki-cli` package's, so the site should install it from its reviewed
  package index, pinned by version, as it installs everything else.
