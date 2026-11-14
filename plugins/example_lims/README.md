# example_lims

An example of a plugin that connects a LIMS: it links each protein to its
record there, imports the LIMS's proteins nightly, and writes Iki's IDs back.
It talks to a small, made-up REST API (see `scripts/lims.py`); to connect a
real LIMS or ELN, copy it and change that file.

It shows the parts of Iki such a plugin needs:

- **`lims_id`**, a field it adds to `protein.Protein`: `unique`, so no two
  proteins in a project have the same one, and a `link` to the record's page
  in the LIMS ([ADR 0082](../../docs/adr/0082-unique-fields-link-records-in-other-systems.md)).
  Anyone can find a protein by it with `GET /entities/lookup`, the SDK's
  `entities.lookup` or the MCP tool `lookup_entities`.
- **`import_from_lims`** creates proteins with `match_on: lims_id`, so a
  record that comes again updates its protein instead of making another,
  and a protein Iki had before the LIMS did takes the record's ID.
- **`import_nightly`**, a scheduled trigger, runs it at 2:00 UTC as the
  service account a project's Admin picks
  ([ADR 0081](../../docs/adr/0081-scheduled-triggers-run-as-a-service-account.md)).
  Each run asks only for what changed since the last one that succeeded.
- **`send_to_lims`** writes proteins' Iki IDs and names to their records,
  and keeps what it wrote with the run (`sent.json`). **`send_renamed`**
  runs it when someone renames a protein in Iki.
- Both actions reach the LIMS's host, and only it, through the egress
  proxy. The deployment names the host in the `lims_host` setting, which
  `network: ["{settings.lims_host}"]` refers to
  ([ADR 0083](../../docs/adr/0083-a-setting-can-name-a-host-an-action-reaches.md)).

Every trigger is off until a project's Admin turns it on there.

## Setting it up

In the deployment's config repository, give the host:

```yaml
# settings/example_lims.yaml
lims_host: lims.example.org
```

and map the `LIMS_TOKEN` secret to an API token the LIMS issued for Iki,
which can read and change proteins. The scripts use only Python's standard
library.
