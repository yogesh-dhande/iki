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
- **Lab roles**: Coordinator, Engineer, Analyst, Reviewer and QA, which a
  project's Admins give its members on the Members page, beside their
  project role
  ([ADR 0116](../../docs/adr/0116-lab-roles-are-declared-by-plugins-and-given-per-project.md)).
  They grant no access by themselves; what needs each is listed with it.
- **`Sample`**, a protein's sample as the lab tests it, with a `status`
  state field
  ([ADR 0125](../../docs/adr/0125-a-state-is-a-field-each-state-says-who-sets-it.md)):
  Registered, Received, In testing, In review, then Approved, Released or
  Rejected. Each state says which lab roles set it, from any other:
  Coordinators register and receive samples, Analysts test and submit
  them, Reviewers approve them, and QA releases or rejects them. Sending one
  back to testing, rejecting it, or registering it again asks why. Approved,
  Released and Rejected lock it: its results can't change, by anyone or
  anything, until someone sets its status back, and each change is kept in
  its history with who made it and why. Keeping the reviewer apart from the
  analyst who submitted it (four eyes) comes with guards, rules a plugin
  declares on top of who may set a state.
- **Who has each sample**
  ([ADR 0122](../../docs/adr/0122-records-name-people-and-each-person-has-their-work.md)):
  its `analyst`, `reviewer` and `qa`, each someone in the project with that
  lab role, and its `due_on` date. Each is assigned the sample in its turn:
  the analyst while its status is Received or In testing, the reviewer while it's In
  review, QA once it's Approved, and it's on their My work, soonest due
  first. A received sample nobody has is open to everyone with the
  Analyst role, who can take it. An approved sample is locked, its people
  with it, so its QA is named before it's approved; if nobody is, it's open
  to everyone with the QA role. Someone who leaves
  the project, or loses the role, stays named, and the sample needs
  reassigning: the project's Admins are told, and reassign it, or many at
  once from the grid.

Every trigger is off until a project's Admin turns it on there.

It also books instruments, to show a component that shows many entities
([ADR 0117](../../docs/adr/0117-components-show-many-entities-in-more-places.md)):

- **`Instrument`** and **`Booking`**, types of its own: a booking is time on
  an instrument, from `starts_at` to `ends_at`, for a `purpose`. An
  instrument has two state fields, set on their own: its `service` (In
  service, Out of service or Retired, which locks it) and its `calibration`
  (Due, Calibrated or Failed). Engineers set both; an Analyst can say it's
  out of service, with a reason, and QA that its calibration is due.
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
