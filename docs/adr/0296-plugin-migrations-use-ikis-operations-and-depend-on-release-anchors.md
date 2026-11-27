# 0296. Plugin migrations use Iki's operations and depend on release anchors

- Status: Accepted
- Date: 2026-11-27
- Refines: [0033](0033-plugin-codegen-and-migrations.md) (what of Django and
  of Iki's own migrations a plugin migration may use),
  [0095](0095-a-plugin-says-which-iki-it-works-with-and-versions-its-changes.md)
  (`iki:` covers a plugin's migrations)

## Context

A plugin's migrations are made when it's written, reviewed in its pull
request and committed with it (ADR 0033). After M1 most of them live in
labs' repositories, which Iki can't edit, and a deployment refuses one
that changed after it was applied. Until now those files were Django's
own:

- **They import Django.** `django.db.models`, `django.db.models.lookups`,
  `django.contrib.postgres.indexes` and more. Django renames and removes
  what they use: `CheckConstraint(check=…)` became `condition=` in 5.1 and
  `check=` was removed in 6.0. Every plugin migration has CHECK
  constraints, so on the first such upgrade after M1, Iki would have to
  break every lab's plugin or stay on an old Django.
- **They depend on Iki's own migrations by name.** First-party migrations
  depended on 16 of them (12 in `iki_entities`, 4 in `iki_files`), most on
  whichever was newest when they were made. Squashing Iki's migrations
  would break every plugin, and nothing tied these names to `iki:`: a
  plugin made on 0.3 that kept `iki: ">=0.1"` validated and built on 0.1,
  then failed `iki deploy build` with Django's missing-node error, reported
  as "a plugin's manifests have changes its migrations don't".
- ADR 0033 declared `iki_server.entities.fields`, `iki_server.entities.checks`
  and `iki_server.plugins.operations` as API, but not the Django
  operations or the migration names the same files use.

Precedent: Rails migrations name the framework version they were written
for (`ActiveRecord::Migration[7.1]`), which Rails keeps working when the
framework changes; Django's own squashed migrations keep a stable name
that later ones depend on; Alembic's branch labels give a stable name to
depend on in another package's history. The alternative, a codemod per
Django upgrade in `iki plugin upgrade`, would be paid at every upgrade and
still can't edit a migration a deployment has applied.

## Decision

**A plugin migration imports one module of Iki's: `iki_server.plugins.operations`.**
`iki plugin makemigrations` writes

```python
from iki_server.plugins import operations as iki


class Migration(iki.Migration):
    dependencies = [
        ('iki_anchors', '0001_iki_0_1'),
    ]

    operations = [
        iki.CreateModel(
            name='Sample',
            fields=[
                ('entity', iki.OneToOneField(on_delete=iki.PROTECT, parent_link=True, …, to=iki.ENTITY)),
                ('name', iki.CharField(db_index=True, max_length=200)),
            ],
            table='acme__lab_sample',
            constraints=[
                iki.CheckConstraint(condition=iki.GreaterThanOrEqual(iki.Length('name'), 1), name='acme__lab_sample__name__not_empty'),
            ],
            bases=(iki.ENTITY,),
        ),
    ]
```

The module has a wrapper for every operation, field, index, constraint,
expression and constant the command writes, and Iki's own operations
(`AppendOnly`, `ConvertUnit`, `RenameEntityType`, `MigrateValues` and the
others ADR 0033 and 0125 name). Each wrapper's name and arguments are
Iki's, and stay as they are when Django changes: the wrapper translates to
whatever the Django of that release needs. Most read like Django's today.
Where Django's are private or in flux, Iki's differ: `CreateModel` takes
`table`, `indexes` and `constraints` rather than a `Meta` options mapping,
and `Q` takes `negated` and `connector` rather than `_negated` and
`_connector`. Iki's own core models are constants (`iki.ENTITY`,
`iki.FILE`, `iki.USER`), so their app labels aren't in plugin files
either. `iki_server.entities.fields` and `checks` are no longer API: their
fields and checks are wrappers too. A migration may also import Python's
standard library, for a backfill's own code.

There's no `RunPython` or `RunSQL`. A plugin changes stored values with
`MigrateValues`, which gives each changed record a revision and an audit
event (ADR 0148); raw Python or SQL could rewrite records without either.

The module lists what it offers in `__all__`, and Iki publishes the same
list, with the release each name came in, in
`iki_spec/migration_api.json`. A name added after the first release is
used only by a plugin whose `iki:` admits no earlier release.

**Plugin migrations depend on release anchors, never on Iki's other migrations.**
The app `iki_anchors` has one migration per minor release,
`0001_iki_0_1`, `0002_iki_0_2`, …, which depends on the last migration of
each of Iki's apps in that release (the core plugin's included) and on the
anchor before it. It changes nothing in the database. Iki never removes,
renames or squashes an anchor; if Iki squashes its own migrations, it
changes what an anchor depends on, never its name. The same table lists,
for each release, its anchor and what it comes after, and the manifest
spec prints it.

`iki plugin makemigrations` writes a dependency on the anchor of the
oldest release the plugin's `iki:` admits, in place of every dependency on
Iki's own migrations that Django would write. So making a migration on a
newer Iki doesn't narrow the releases a plugin works with. The command
checks that every model of Iki's the migration refers to is there at that
anchor; when one isn't (a core type added in 0.3, extended by a plugin
that says `>=0.2`), it refuses, naming the release that adds it.

**The anchor of a release in development stays after all of Iki's migrations.**
While Iki is a development build of a new minor release (`0.2.0.dev3`), a
test checks that its anchor is the table's last and comes after the newest
migration of every one of Iki's apps. A change that adds one of Iki's
migrations updates the table, or fails, with the line to add. When X.Y.0
is released, its anchor is fixed: a patch release's migrations come after
it, and plugins made for X.Y don't need them. A patch release adds nothing
plugins depend on.

**`iki plugin validate`, `build` and `iki deploy build` check each migration.**
Without Django or a database, from the files themselves, the validator
refuses (with the file, line and column):

- an import of anything but `iki_server.plugins.operations` and the
  standard library (`migration-import`), naming the wrapper for each name
  used from it: "`django.db.models.lookups.GreaterThanOrEqual` is
  `iki.GreaterThanOrEqual`";
- a name the module doesn't have (`migration-unknown-name`), and a
  `Migration` that isn't `iki.Migration` or lists its dependencies other
  than as `(app, migration)` pairs (`migration-shape`);
- a dependency on one of Iki's migrations that isn't an anchor
  (`migration-core-dependency`), naming the anchor to use;
- an anchor this Iki doesn't have (`migration-newer-iki`): the migration
  was made with a newer Iki;
- an anchor, or a wrapper, from a release later than the oldest `iki:`
  admits (`migration-iki-range`), naming that release: "depends on Iki
  0.2's anchor, and `iki:` admits 0.1: raise it to `>=0.2`".

`iki deploy build` stops on these before it checks that the migrations
match the manifests, and that check says a plugin's manifests have changed
only when they have. The migrate Job still refuses a migration that needs
an anchor it doesn't have, naming the release.

**Iki's own migrations stay Django's.** The core plugin's migrations are
Iki's, made by `make core` with the same history rules and kept in the
server (ADR 0038); like `iki_entities`'s, they change with Iki and depend
on Iki's migrations by name.

What ADR 0033 says of history is unchanged: history is never deleted, a
history column only widens, renames are asked once, unit changes convert
values, type renames follow through, and history tables are append-only.

## Consequences

- Positive: a Django upgrade changes Iki's wrappers, not labs' committed
  migrations, and Iki can squash its own migrations without breaking a
  plugin.
- Positive: a plugin whose migrations need a newer Iki than its `iki:`
  admits is caught where it's written, naming the release, rather than as
  a missing node at a deployment's build.
- Positive: a reviewer reads one vocabulary, and data changes in a plugin
  migration always leave revisions.
- Negative: Iki maintains a wrapper for every construct `makemigrations`
  writes, and a round-trip test that every first-party migration reads
  back to the operations it was written from. A Django feature a plugin
  needs waits for its wrapper.
- Negative: a change that adds one of Iki's migrations during a release
  also updates the anchor table.
- Negative: before M1, every first-party plugin's migrations were made
  again from scratch, as one initial migration each: databases made
  before can't be migrated, which is allowed until M1.

## Alternatives considered

- **Codemods for each Django upgrade, and one supported Django per LTS
  line.** Rejected: the cost recurs at every upgrade, and a codemod can't
  change a migration a deployment has applied.
- **An anchor in each of Iki's apps** (`iki_entities.0029_anchor_0_1`).
  Rejected: an app's next migration follows its anchor, so the anchor of a
  release in development would have to be moved past every new migration;
  one app of anchors depends on all of them and is never in the way.
- **Depending on the anchor of the release that made the migration.**
  Rejected: every migration made on a newer Iki would raise the plugin's
  oldest release, whether it needed anything new or not.
- **Keeping `iki_server.entities.fields` and `checks` as API beside the
  operations module.** Rejected: two more modules whose internals labs'
  files would pin, for no gain over wrappers.
