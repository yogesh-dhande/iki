# example_lims

An example of a plugin that connects a LIMS: it links each protein to its
record there, imports the LIMS's proteins nightly, and writes Iki's IDs back.
It talks to a small, made-up REST API (see `scripts/lims.py`); to connect a
real LIMS or ELN, copy it and change that file.

It shows the parts of Iki such a plugin needs:

- **`lims_id`**, a field it adds to `iki/protein.Protein`: `unique`, so no two
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
- **Lab roles**: Analyst, Reviewer and QA, which a project's Admins give
  its members on the Members page, beside their project role
  ([ADR 0116](../../docs/adr/0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)).
  They grant no access by themselves; what needs each is listed with it.
- **`Sample`**, a protein's sample as the lab tests it, with states
  ([ADR 0118](../../docs/adr/0118-a-record-moves-through-its-types-states.md)):
  Registered, Received, In testing, In review, then Approved, Released or
  Rejected. Analysts receive, test and submit it; a Reviewer who didn't
  submit it approves it or sends it back, with a reason; QA, who neither
  submitted nor approved it, releases or rejects it. Approved, Released and
  Rejected lock it: its results can't change, by anyone or anything, until
  it's sent back or reopened, and each move is kept in its history with who
  made it and why.

Every trigger is off until a project's Admin turns it on there.

It also books instruments, to show a component that shows many entities
([ADR 0117](../../docs/adr/0117-components-show-many-entities-in-more-places.md)):

- **`Instrument`** and **`Booking`**, types of its own: a booking is time on
  an instrument, from `starts_at` to `ends_at`, for a `purpose`.
- **`booking_calendar`**, a component that draws bookings by day, a month at
  a time. It's a tab on each instrument's page (its bookings), a way to show
  the grid of bookings ("Show as: Calendar"), a section of each project's
  page, and a section of each person's home with the bookings they made
  (`{{ me }}`). Iki runs it in a sandboxed frame and gives it the bookings
  each place's search finds, as the person viewing it may read them, with
  only the four fields it reads. It asks for one month at a time with an
  `iki-refine` event, which can only narrow what its place shows, and Iki
  opens a booking's page when it's picked. It reaches nothing and changes
  nothing: bookings are made and changed in Iki's own pages, as any entity.

A booking that starts in one month and ends in the next shows on the day it
starts.

## Setting it up

In the deployment's config repository, give the host:

```yaml
# settings/example_lims.yaml
lims_host: lims.example.org
```

and map the `LIMS_TOKEN` secret (key `iki__example_lims.LIMS_TOKEN`) to an API token the LIMS issued for Iki,
which can read and change proteins. The scripts use only Python's standard
library.
