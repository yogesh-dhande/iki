# 0096. Bundles carry entities between deployments, with their history

- Status: Accepted
- Date: 2026-11-15
- Refines: [0015](0015-gitops-config-and-export-import-bundles.md)

## Context

ADR 0015 says data moves between deployments only in explicit bundles: a
lab validates reference data on staging (primers, cell lines, standard
sequences) and promotes it to production. In a regulated lab the record
that moves has to stay the record: who made each revision and when, what
each was, the files it named, and the runs that made it (ALCOA+). A copy
that starts its history again on arrival is a new record, not the
validated one.

Each deployment numbers its own HRIDs from 1 (one sequence per prefix), and
provisions its own people through SSO. Entities, revisions, files and runs
have UUIDv7 IDs, unique across deployments.

Precedent:

- GitLab's project export is a versioned archive of JSON per relation and
  the uploads, imported into a new project, with members matched by email.
- GitHub Enterprise Importer keeps each record's author. Authors with no
  account on the target become _mannequins_, placeholder users that an
  admin later claims for a real account.
- Jira and Benchling renumber issue keys and registry IDs that are taken
  on the target, and keep the old one on the record.

## Decision

**A bundle is a signed archive of entities from one project.** An admin of
the project picks what goes: a collection, a search, or entities by hand.
The bundle also takes each entity they reference, if it's in the same
project, so it arrives whole. It holds, for each entity:

- every revision, with who made it, when, through what (web, API, a run,
  an agent) and why;
- the values at each revision, by type and field name, as history keeps
  them, removed fields included;
- the files any revision names, their bytes once each by SHA-256;
- the runs that made or changed it, with their inputs, parameters, seed and
  plugin commit, and its lineage.

A reference to something that doesn't go, such as a run's input in
another project or a lineage parent left out, is kept as it was (the UUID,
HRID, type and revision) and listed in the bundle as _outside_, so the gap
is visible on the target, not silent. Purged entities don't go.

The archive is `.tar.gz`, readable with standard tools. `bundle.json`
describes it: the format (`bundle/v1`, a spec schema like the others), the
source deployment and its Iki release, who exported it, when and why, the
plugins and schema versions of its types, and the SHA-256 of every other
member. `bundle.json.sig` is an Ed25519 signature of `bundle.json` by the
source deployment's bundle key, so one signature covers every byte. Each
NDJSON member's lines have a schema too (`bundle/v1`'s `$defs`): people,
agents, files, entities and what's outside.

**A deployment imports only bundles from deployments it trusts.** Each
deployment has a bundle signing key (`IKI_BUNDLE_SIGNING_KEY`, a secret
like its other keys), and its configuration lists the public keys of the
deployments it accepts bundles from, by name (`bundles.trusted`, reviewed
in its GitOps repository). A bundle that isn't signed, or is signed by a
key the configuration doesn't list for the deployment the bundle names, is
refused. A reader trusts nothing in an archive until it checks out: it
extracts nothing to disk, accepts only the members a bundle has, each
listed with its SHA-256 and size, and limits what it unpacks
(`IKI_BUNDLE_MAX_BYTES`). `iki-server verify_bundle` runs these checks
alone.

A registered file (ADR 0055) goes as its address, object version and
SHA-256, not its bytes: it stays where it was registered, and the importing
deployment must be able to read it there.

**An import is planned, then applied whole.** An admin uploads the bundle
and picks the project it goes into. Iki checks it and shows the plan before
anything changes:

- every type is installed, active, and at the bundle's schema version or
  later; otherwise it says which plugin to deploy first;
- each entity is new, already here as it is (skipped), or here at an
  earlier revision from an earlier import, when the bundle's newer
  revisions are added after it. One changed here since it was imported has
  diverged, and is a conflict: Iki won't merge two histories;
- each value a unique field or an identity can't share with an entity
  already in the project is a conflict;
- each person is matched by email to someone on this deployment. Anyone
  else becomes a mannequin: a person with their name and email who can't
  sign in, shown as "imported". When the identity provider later
  provisions someone with that email, they take over the mannequin's
  records, as GitHub's reclaiming does;
- HRIDs this deployment has already used are renumbered from its own
  sequences. The entity keeps its source HRID, and its page says "SMP-12 on
  staging". Lookups by HRID, such as an alignment's rows, also match it.

The bundle is uploaded as files are (ADR 0036), in parts straight to the
file store, with `POST /projects/{id}/imports` and the import's own
`parts` and `complete`. A task then reads it and writes the plan:
`GET /imports/{id}` gives where it's from, its counts, types, files and
what it refers to outside it, and the problems that keep it from being
applied. `/imports/{id}/entities` and `/people` list each one's fate. An
untrusted or broken bundle is refused, with why. An import that isn't
applied can be discarded, and its archive is deleted. Planning, refusals
and discards are in the audit log (`bundle.import_planned`,
`bundle.import_refused`, `bundle.import_discarded`).

The admin confirms with a reason, and the whole import is applied in one
transaction, or not at all, as bulk imports are (ADR 0056). Imported
revisions keep their authors, times and reasons. The import is a record of
its own (the bundle's SHA-256, its source, who imported it, when and why),
and each imported entity's page links to it. Imported runs are history: they
read as they ran, and can be reproduced when this deployment has the
plugin's commit, as any run can.

A project's Admin exports from its settings, or with
`POST /projects/{id}/bundles`: a collection (a static one with its members,
a dynamic one with what its search finds for them now in the project), or
entities by ID or HRID, with a reason. A task writes the bundle in the
background, and keeps it in the file store for a week, as GitHub keeps
migration archives. Then `maintain` deletes it, unless the project is under
a legal hold (ADR 0008). A download is a short-lived link
(`GET /bundles/{id}/content`).

Exports and imports are in the audit log, with the bundle's SHA-256:
asking for an export (`bundle.export_requested`), writing it
(`bundle.exported`), each download (`bundle.downloaded`), and importing
(`bundle.imported`). Exporting needs the project's Admin role, and
importing an org admin who is also an Admin of the target project. Tokens
need the `admin` scope.

## Consequences

- A validated record arrives as itself: its revisions, authors, files and
  provenance, checkable against the source by the bundle's signature.
- A bundle can be imported again after the source changes: newer revisions
  are added, and nothing already imported changes.
- Deployments that exchange bundles have to trust each other's keys, in
  configuration, which is reviewed like the rest of it (ADR 0015).
- HRIDs differ between deployments. The source HRID is kept on the entity
  and in the plan, so people can find one from the other.
- `bundle/v1` is a compatibility surface: a deployment reads bundles from
  older Iki releases, and refuses ones from newer releases it can't read.
