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
- **Lab roles**: Coordinator, Engineer, Analyst, Reviewer and Sample and result QA, which a
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
  them, Reviewers approve them, and QA releases or rejects them. Rejecting a
  sample or registering it again requires a reason. Saving a correction to an Approved sample
  also returns it to In testing and records the editor and reason. Released
  and Rejected lock its values until an authorized person changes the status.
- **Electronic signatures** under 21 CFR Part 11
  ([ADR 0126](../../docs/adr/0126-an-electronic-signature-is-bound-to-the-revision-it-signs.md)):
  setting a sample In review is its analyst's signature of authorship,
  Approved the reviewer's, and Released or Rejected QA's. Each signer signs
  in again at the identity provider
  ([ADR 0124](../../docs/adr/0124-signing-signs-in-again-at-the-identity-provider.md)),
  and the sample's page shows each signature: who, when, and what it means,
  checked again on request. A sample's bundle carries them, and
  `iki bundle verify` checks them anywhere.
- **Guards** on its status
  ([ADR 0128](../../docs/adr/0128-guards-say-when-a-state-may-be-set.md)),
  conditions on top of who may set a state. A sample moves along its process:
  it's submitted for review from In testing, once its result is recorded, and
  approved from In review, released once approved, and rejected once in review
  or approved. Whoever submitted it for review can't approve it, and whoever
  approved it can't release it (four eyes). Recording or changing its purity,
  concentration, result or testing notes since it entered In testing also
  disqualifies the editor from approval and release. A reviewer can still
  claim their assignment. Guards are checked as each
  signature is given, with its signer acting, so nobody signs a change a guard
  refuses. Where one won't let someone sign, the sample's page says why.
- **Rules** checked at every save
  ([ADR 0127](../../docs/adr/0127-plugins-rules-are-checked-at-every-save.md)).
  A sample's purity (at least 95%) and concentration (0.5 to 10 mg/mL) are
  its specification: a result outside it is saved as measured, with why, and
  the sample is held until someone with the QA role, other than whoever saved
  it, reviews it. While it's held it can't be approved, released or rejected.
  A sample whose purity is out of specification can't pass, and its reviewer
  isn't its analyst. The sample's form checks each rule as you type.
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
- **Who hears of each sample**
  ([ADR 0129](../../docs/adr/0129-types-declare-their-notifications.md)):
  everyone with the QA role in its project when it's submitted for review,
  and its analyst when QA rejects it, in Iki and by email. A rejection is
  required: nobody can turn it off in Iki. Anyone who can read a sample can
  watch it, to hear of both too, or mute it. Each person is told only while
  they can read it, and not of their own change; emails say what happened
  and link to it, without its values.

- **Requests to an analytical core**
  ([ADR 0132](../../docs/adr/0132-a-request-is-owned-by-the-project-that-asks-and-served-by-another.md)):
  an **`AnalysisRequest`** (`REQ-…`) is a project's request to a core
  facility to test samples of a protein. The project asking writes its
  title, samples, analyses, the date it needs them by, and instructions,
  then sends it to a core that takes analysis requests from it (a core's
  Admins choose which projects, under Settings → Requests it takes). It goes with its
  samples and protein, and the core reads those and nothing else of the
  project's. The core writes only its own fields: `status` (Submitted,
  Accepted, In progress, Delivered or Declined), `priority`, `scheduled_on`,
  `analyst` and `core_notes`, each set with the lab roles its people hold in
  the core's project; the project asking sets everything else, and its own
  `standing` (Open, or Withdrawn, with why). Neither side can write the
  other's, through the API, a run, an import or SQL. The core's queue is
  its Requests tab ("Sent to us"), by status, assignee and the project that
  asked, and on its people's My work; the project asking follows what it
  sent there too ("We sent"), by status and core.
- An **`AnalysisResult`** (`ANR-…`) starts as a Draft in the core. To submit
  it, the analyst names the sample, records pass or fail, and enters the
  measurement for the chosen analysis. SEC purity and peptide map sequence
  coverage are percentages, intact mass is in atomic mass units, and
  endotoxin is in EU per mg of protein. The analyst signs for authorship,
  which locks the values and puts the result In review. A reviewer other
  than the creator, submitting analyst and anyone who edited values since
  the latest Draft signs to set it Reviewed. An Analyst, Reviewer or QA can
  correct a submitted or reviewed result. The form shows that saving will
  return it to Draft and asks for a reason. The correction and return appear
  together in history. The corrected values need fresh submission and review.
  QA must also be independent of the creator, submitting analyst, reviewer
  and value editors to sign the reviewed revision. The project that asked
  receives the locked release. Released is final.
- **How much of each there is**
  ([ADR 0139](../../docs/adr/0139-a-records-amount-is-a-ledger-of-what-moved.md)):
  a sample's volume, in µL (shown in mL), and each aliquot's. A sample is
  drawn from by volume, or by protein mass at its concentration (50 µg of
  a 2 mg/mL sample is 25 µL), and split into aliquots: each a new
  `Aliquot`, naming its sample, holding exactly what the sample lost. Two
  analysts drawing at once take turns, and neither takes more than there
  is. Each draw, transfer, split and adjustment is in the sample's ledger,
  with who, when and why. A Registered sample, not in the lab yet, and a
  Rejected one hold their volume: none of it can be drawn or split off.

- **Donors, and what's derived from them**: a `Donor` (by the study's
  pseudonymous code, never a name) gives `Sample`s, each sample `Aliquot`s,
  and each aliquot `Result`s, each naming the one before. A donor's
  `consent` is a state field: Given, or Withdrawn, which locks the donor;
  Coordinators set it, saying why.
- **`hold_withdrawn_consent`**, a trigger on a donor's consent set to
  Withdrawn, runs **`hold_withdrawn`** with every aliquot and result in the
  project derived from the donor, through their samples and anything runs
  made from those, across the graph of what depends on what
  ([ADR 0123](../../docs/adr/0123-queries-walk-the-graph-of-what-depends-on-what.md)).
  It puts each on hold, saying whose consent was withdrawn; it takes
  nothing off hold, which is for a person. It's all of them or none: past
  1,000 of either, the firing fails and says so. The samples themselves
  keep their status: one that's Approved or Released is locked, and QA
  decides what happens to it. Everything derived from a donor is on the
  donor's Lineage page, under "Used by", and `downstream:DNR-000001` in
  search finds it too.

Every trigger is off until a project's Admin turns it on there.

It also books instruments, to show a component that shows many entities
([ADR 0117](../../docs/adr/0117-components-show-many-entities-in-more-places.md)):

- **`Instrument`** and **`Booking`**, types of its own: a booking is time on
  an instrument, from `starts_at` to `ends_at`, for a `purpose`. No two
  bookings of an instrument overlap, unless one is `cancelled`: the database
  refuses the second, whatever writes it, and says which booking it clashes
  with (`no_overlap`). One can start as another ends. A booking ends after
  it starts, and is of an instrument that's in service. An
  instrument has two state fields, set on their own: its `service` (In
  service, Out of service or Retired, which locks it) and its `calibration`
  (Due, Calibrated or Failed). Engineers set both; an Analyst can say it's
  out of service, with a reason, and QA that its calibration is due. A
  booking names who it's for (`booked_by`), who hears when its instrument
  goes out of service before the booking ends.
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

And it keeps material where it is
([ADR 0180](../../docs/adr/0180-where-material-is-and-how-its-received-moved-and-counted.md)):
**`Freezer`**, **`Shelf`**, **`Rack`**, **`Box`** and **`Tube`**, each declaring
`storage:`, what it holds and its positions.

- A rack's 20 slots are numbered 1 to 20 (`grid: {rows: 5, columns: 4, labels:
number}`); a box's rows and columns are fields set as it's made and never
  changed (`immutable: true`), so a 9 × 9 box's positions are A1 to I9. Iki
  refuses a position outside the grid, or one that's taken, and a swap is one
  change. Each move says why, and is in the tube's location history and the
  audit trail.
- A freezer's asset tag, a box's or rack's barcode and a tube's 2D code are what
  a scanner reads (`barcode:`): a scan anywhere on the storage pages opens it.
  Labels print with them as DataMatrix or QR codes and Code 128, from the
  browser or as ZPL for a Zebra printer.
- A tube's volume is its amount (`amount: {unit: uL}`), drawn at the bench.
  `confirm: [draw]` asks for its barcode to be scanned first, and each draw
  names what it went into. `follows: [lot, sample]` makes its lot's draw
  guards and its sample's holds its own: a tube of an expired
  [`iki/lots`](../lots/README.md) lot gives nothing, and one due for retest
  only a QA sampling draw.
- An **`AnalysisRequest`** sends its `tubes` with it, and the core's
  Coordinators receive them against that list (`receives: {field: tubes, by:
[coordinator]}`): each matched, missing, extra, damaged or rejected, put away
  into the next free positions, and whoever asked is told of anything that
  didn't arrive as expected.

## Setting it up

In the deployment's config repository, give the host:

```yaml
# settings/example_lims.yaml
lims_host: lims.example.org
```

and map the `LIMS_TOKEN` secret (key `iki__example_lims.LIMS_TOKEN`) to an API token the LIMS issued for Iki,
which can read and change proteins. The scripts use only Python's standard
library.
