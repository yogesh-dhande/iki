# 0034. Each deployment pins its plugins in a lockfile, and builds its own image

- Status: Accepted
- Date: 2026-11-10
- Refines: [0006](0006-deploy-time-plugins-as-django-apps.md),
  [0033](0033-plugin-codegen-and-migrations.md)

## Context

ADR 0006 installs plugins only at deploy time, from a `plugins.lock.yaml`
in each deployment's config repository. This ADR settles what the lockfile
holds, how a deployment's plugins get into the image it runs, and what is
checked on the way. The deployments are pharma and lab ones. Their quality
teams need to see exactly which code each environment ran, and to know that
a validated build doesn't change by itself.

## Decision

**The lockfile pins every plugin to a full git commit.** Each entry gives
the plugin's name, its repository (`git`), its directory there (`path`, if
not the root), the commit (`sha`, all 40 or 64 hex digits), its `state`, and
optionally a settings file. A branch or tag can be moved; a commit can't.
So the same lockfile always builds the same plugins, and reviewing a change
to the lockfile is reviewing what changes in production. `state` is
`active` for now; ADR 0008's detach and purge add `detached` and `purged`.

**The core plugin isn't listed.** Its manifests ship inside `iki-spec`
(`iki_spec/core`), are written and checked like any plugin's, and are
released at Iki's version. Other plugins use its types without declaring a
dependency. It holds `core.Entity` and `core.Collection` (ADR 0038); `core.View` and
`core.App` follow.
Since it can't be released apart from Iki, it isn't one of the first-party
plugins under `plugins/`.

**`iki deploy build` turns a lockfile into the image's plugins.** It runs in
the config repository's CI:

1. It reads the lockfile and refuses problems the way `iki plugin validate`
   reports them, with line, column and hint.
2. It fetches only the pinned commits, into a cache of bare repositories,
   and exports each commit's tree. It never runs a plugin from a working
   copy.
3. It validates every plugin with all the others, and checks they can be
   installed together (ADR 0033).
4. It checks the deployment's settings for each plugin and fills in
   defaults.
5. It checks that each plugin's migrations are complete. A plugin whose
   manifests changed without a migration would fail at deploy, so it fails
   here.
6. It writes the apps (ADR 0033), each plugin's `settings.json`, and
   `iki_installed_plugins/lock.json`, which records where each plugin came
   from and the hashes of the lockfile and settings files. The server shows
   this record, and audits can read it.

The output is byte-for-byte repeatable.

**Git is run defensively.** Git can be pointed at transports that run
commands (`ext::`) or read the build machine's files (`file://`), so it may
only use https and ssh, even after `insteadOf` rewrites. It never prompts,
so CI fails rather than hangs. It runs no hooks, submodules or LFS. It
checks that the fetched commit is the one asked for. Credentials come from
git's usual helpers, as CI sets them up; the lockfile holds none.

**A deployment's image is Iki's server image plus a layer of plugins.** The
server image puts `/opt/iki/plugins` on its import path.
`deploy/plugins.Dockerfile` copies the build there, read-only, and compiles
it, starting from the server image by digest. This is how Keycloak and
Grafana images are extended, and it means the deployment's own registry,
signing and admission policy apply to the result.

**The server refuses builds it can't trust.** A plugin build records the Iki
version that made it. A server of any other version refuses to start, since
generated models and migrations change between versions.

**Local plugins are for development only.** A lockfile entry may name a
local directory (`local`) instead of a commit, for working on a plugin in
the dev cluster. The build refuses it without `--allow-local`, and records
that it did. A server only starts with such a build when
`plugins.allowLocal` (`IKI_ALLOW_LOCAL_PLUGINS`) is set, as the dev cluster
sets it. The dev cluster builds its image this same way, from
`deploy/dev/plugins.lock.yaml`.

**Rolling back is deploying an older build, and it never loses data.** The
migrate Job runs `migrate_deployment` instead of `migrate`, on install,
upgrade and `helm rollback`. An older build doesn't have the newer plugin
migrations the database has applied, and Django would just ignore them. So
the Job keeps a copy of every plugin migration it applies
(`iki_plugin_migration`, which the app can only read), each with its hash.
When a build lacks applied migrations, the Job reverses them from those
copies. First it checks what reversing would destroy:

- every operation must be reversible
- no table it would drop may hold rows
- no column it would drop may hold anything but its default, or, in a
  history table, anything at all
- no column whose type it would change back may hold values

If any check fails, it changes nothing and says what would be lost, and
restoring the backup taken before the upgrade is the way back. A rollback
right after a bad deploy, before anyone used the new fields, goes through.

It also refuses:

- a database a newer Iki migrated
- a plugin that disappeared from the lockfile: removing one is ADR 0008's
  detach
- an applied migration whose file has changed: a change belongs in a new
  migration

**Each company keeps one config repository for its deployments.**
[`deploy/config-template`](../../deploy/config-template) is a starting
point. Each deployment directory holds:

- `iki.yaml`: the Iki release, as version, server image and chart
- the lockfile, and a settings file per plugin
- Helm values
- `image.yaml`: the image CI built

Its workflow checks every pull request with `iki deploy build`. On merge,
it checks Iki's signature on the server image, then builds, signs and
attests the deployment's image. Then it opens a pull request that points
`image.yaml` at the new image, so deploying is itself a reviewed merge.
Argo CD or Flux applies the chart from there. This is the GitOps flow
enterprises already use for their other services, with the audit trail
that validated environments need.

## Consequences

- Promoting from staging to production means copying lockfile lines. What
  production runs can be read from its config repository, and from the
  running server.
- A deployment needs CI that can read its plugin repositories and push an
  image. That's the CI it already has for its config.
- Fetching a commit by SHA needs a git server that allows it. GitHub,
  GitLab and Bitbucket do.
- Upgrading Iki means rebuilding the plugins with the new `iki`, in the same
  change that bumps the server image.
- A plugin author who wants rollbacks to keep working writes migrations
  that can be reversed: `RunPython` and `RunSQL` with a reverse.
- Plugin migrations applied before Iki kept copies can't be rolled back
  automatically. Only development databases have them.
