# 0169. An action names the settings it can't run without

- Status: Accepted
- Date: 2026-11-20
- Refines: [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0040](0040-action-and-environment-manifests.md),
  [0054](0054-run-secrets.md)

## Context

A plugin's settings are set per deployment, in its config repository, and
every run gets them all (ADR 0034). A setting is either `required`, so a
deployment can't build without it, or has a `default`, or neither: then a
deployment may leave it unset. That third kind is for settings only some of
a plugin's actions need. The dna plugin's `ncbi_email` is one: NCBI asks
automated clients for a contact address, so fetching from NCBI needs it,
while importing a file and aligning reads don't. Making it required would
stop a deployment that never fetches from installing the plugin at all.

Nothing said which actions needed such a setting. On a deployment that left
`ncbi_email` unset, a scientist could fill in the fetch form and start a run;
the server queued it, a worker took it, and it failed at once, saying the
setting wasn't set. The person who could fix it, an Iki administrator, heard
of it only if the scientist passed the failure on, and the run, its worker
time and its failed attempt stayed in the record for what was never going to
work.

A required secret the deployment hasn't set already keeps its action from
running, and says so, where people launch it (ADR 0054). Settings had no
equivalent.

## Decision

**An action names the settings it can't run without**, under
`requires_settings`: `requires_settings: [ncbi_email]`. Each is one of its
plugin's settings declared `required: false` without a default, since one
every deployment has is no prerequisite. A setting must declare whether it
is required or provide a default. The plugin's checks refuse one the plugin
doesn't declare (`requires-unknown-setting`) and warn of one that's always
set (`requires-setting-always-set`).

**Until the deployment sets them, the action isn't available**, as with a
required secret. A setting that's unset, or set blank, counts as unset. Its
`unavailable_reason` names the plugin and the settings, and says an Iki
administrator sets them in the plugin's settings, in the deployment's config
repository. So:

- the new-run form lists it among the actions that can't run here, with
  why, and, opened for it from a link, says why before anything is filled in;
- a launch, from the app, the API, a workflow or a trigger, is refused with
  the same reason, before anything is queued;
- a workflow with such a step is unavailable, for the same reason.

A self-hosted action is checked too: its runs get the settings as any other
run's do.

**A deployment is told at build time.** `iki deploy build` and `iki plugin
validate --settings` warn, for each such setting left unset, which actions
can't run (`setting-needed`), with the setting's description. It's a
warning, not an error: a deployment that doesn't fetch from NCBI needn't set
`ncbi_email`.

## Consequences

- A run that can't work isn't started: no worker time, no failed attempt,
  and the scientist learns why, and who can fix it, before they fill in the
  form.
- An administrator learns at build time which actions their settings leave
  off, rather than from a scientist's failed run.
- A plugin author declares the dependency once, in the action's manifest.
  An action that reads a setting it doesn't declare still fails in its run,
  as before; the script's own check stays as a backstop.
- Actions built before this need no settings: a run's kept action reads as
  needing none.

## Alternatives considered

- **Make such settings required.** Simple, but a deployment would have to
  set an NCBI contact address to install the dna plugin even if it only
  imports files.
- **Check in the worker before the command starts.** It would fail faster,
  but still after the run was queued and a worker taken, and still tell only
  the person who started it.
- **Infer it from what the command reads.** A command reads settings from
  its input at run time; nothing before then knows which.
