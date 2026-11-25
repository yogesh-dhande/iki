# ikisci: Iki's Python SDK

Work with [Iki](https://ikisci.org) from notebooks and scripts: search and read
entities, change them on the record, run actions and wait for them, and turn
what you find into pandas DataFrames.

```sh
pip install 'ikisci[pandas]'    # or just ikisci, without DataFrames
```

```python
import iki

iki.login("https://iki.example.com")  # once: you enter a code in your browser

proteins = iki.entities.search("iki/protein.Protein", project="kinase-atlas")
table = proteins.to_pandas()  # one row per entity, a column per field
```

Everything you do is recorded in Iki as done by you, through the API, with this
SDK. Your organization's audit log and each entity's history show it.

## Signing in

`iki.login()` signs in with the device flow, as Iki's CLI does. It shows a code,
opens Iki in your browser (when it can), and you enter the code there. You sign
in to Iki with your organization's single sign-on, so the SDK never sees your
password. In Iki you choose what to allow:

- `read`: see projects and read their data
- `write`: create and change entities
- `run`: run actions

You can allow every project or only the ones you pick, and you can end the
sign-in at any time in Iki, under **Authorized apps**, or with `iki.logout()`.

The sign-in is kept in your system's keychain: macOS Keychain, Windows
Credential Manager, or the Secret Service on Linux. Where there's none, as on
many servers and clusters, it's kept in a file only you can read
(`~/.config/iki/credentials.json`). Access tokens last an hour and are renewed
as needed.

For scripts and pipelines, set `IKI_URL` and `IKI_TOKEN` (a service account's
token, say) instead. Nothing is stored then.

An AI agent using the SDK may say what model it is, with
`iki.Client(agent_model="claude-opus-5-5")` or `IKI_AGENT_MODEL`. What it
changes then records that, labelled as the agent's own word, next to you and
the app it signed in as.

Iki is reached only over HTTPS (plain HTTP works only on this machine), with
your system's trusted certificates, so it works behind corporate proxies that
inspect TLS.

## Entities

```python
# Search: a type (with its subtypes), a project (its slug or ID), text, conditions.
found = iki.entities.search(
    "iki/protein.Protein",
    project="kinase-atlas",
    q="kinase",
    where={
        "all": [
            {"field": "length", "op": "gte", "value": 300},
            {"field": "organism", "op": "eq", "value": "TAX-000001"},  # Homo sapiens
        ]
    },
    sort="-created_at",  # newest first
)
for protein in found:  # fetched a page at a time
    print(protein["hrid"], protein["values"]["name"])

found.first()  # the first, or None
found.to_pandas(limit=1000)  # a DataFrame, indexed by HRID

abl1 = iki.entities.get("PRT-000123")
# By a unique field, such as the ID a LIMS gives its record: one per project, at most.
[sample] = iki.entities.lookup("lab.Sample", "lims_id", "S-0042", project="kinase-atlas")

made = iki.entities.create(
    "iki/protein.Protein",
    {"name": "ABL1", "sequence": "MLEICLKLVG"},
    project="kinase-atlas",
    reason="From the paper's table 2",
)

# A change names the revision you read. If someone changed it since,
# iki.Conflict is raised and nothing changes: read it again, then decide.
iki.entities.update(
    "PRT-000123", {"name": "ABL1 kinase"}, revision=abl1["revision"], reason="Full name"
)
iki.entities.archive("PRT-000123", revision=2, reason="Duplicate of PRT-000099")

# A state field is set on its own, to a state its lab roles may set:
# sample["values"]["status"], and sample["can"]["states"]["status"] with why not.
sample = iki.entities.get("SAM-000012")
iki.entities.set_state(
    "SAM-000012", "status", "in_testing", revision=sample["revision"], reason="Rerun"
)

# A locked result may allow a correction. The same update returns it for review,
# and history records the reason alongside the state change and the edited values.
result = iki.entities.get("ANR-000012")
if result["can"]["correct"]:
    iki.entities.update(
        result["id"],
        {"purity": 97.5},
        revision=result["revision"],
        reason="Corrected the transcribed purity from the instrument report",
    )

# Read the measured values at the revision QA signed for a request.
for release in iki.entities.request("REQ-000007")["result_releases"]:
    if release["signed_revision"] is not None:
        values = iki.entities.revision(release["entity"], release["signed_revision"])["values"]

# Fields can name people (ADR 0122), by ID or email address; a state that locks
# a record locks whom it names too.
iki.entities.update("SAM-000013", {"reviewer": "ana@example.com"}, revision=4)
client = iki.client()  # uses the current sign-in
for item in client.work.mine(type="iki/samples.Sample"):  # yours now, soonest due first
    print(item["entity"]["hrid"], item["fields"], item["due"])
for item in client.work.open():  # nobody's yet, and open to your lab roles
    record = item["entity"]
    iki.entities.take(record["hrid"], item["fields"][0], revision=record["revision"])
# Many at once, each its own revision; what can't change is skipped, with why.
bos = {"field": "analyst", "op": "eq", "value": "bo@example.com"}
late = list(iki.entities.search("iki/samples.Sample", where=bos))
iki.entities.assign(late, "analyst", "ana@example.com", reason="Bo is away")["skipped"]
mine = {"field": "assigned", "op": "eq", "value": "{{ me }}"}  # whoever searches

# Its electronic signatures (21 CFR Part 11), each as people read it, checked again.
# They're read and checked here; only a person signing in again in the web app signs.
for signature in iki.signatures.list("SMP-000012", verify=True)["items"]:
    print(signature["manifestation"], signature["verification"]["valid"])
# What you were told (ADR 0129), how you're told of each kind, and what you watch.
for n in client.notifications.list(unread=True):
    print(n["title"], [r["hrid"] for r in n["records"]])
client.notifications.choose({"iki/samples.Sample.in_review": "in_app"})
client.notifications.choose({"iki/samples.Sample.in_review": "off"}, project="stability")
client.notifications.watch("SAM-000013")  # or "muted", or "default"

# Requests between projects (ADR 0132): sent to a core that takes them, for good,
# with the samples it names. The core writes its own fields; you write yours.
core = iki.projects.services("kinase-atlas", type="iki/requests.PlainRequest")[0]
lot = {"title": "Lot 12", "materials": ["<sample UUID>"]}
asked = iki.entities.create("iki/requests.PlainRequest", lot, project="kinase-atlas")
iki.entities.submit(asked["hrid"], core["project"], revision=asked["revision"], reason="Lot 12")
iki.entities.request(asked["hrid"])["results"]  # what the core released back
queue = {"field": "sent_to", "op": "eq", "value": core["project"]["id"]}  # the core's queue

iki.entities.history("PRT-000123")  # who changed what, when, how and why
iki.entities.provenance("PRT-000123")  # where each value came from
iki.entities.origins("DNA-000042")  # the database record or file it was imported from

# Across the graph (ADR 0123): what it was derived from, and what was derived from it,
# through references, runs and files, nearest first, each with the path that reached it.
for item in iki.entities.upstream("RES-000004"):
    print(item["depth"], item["entity"]["hrid"], [s["kind"] for s in item["path"]])
impact = iki.entities.downstream("TUB-000001", type="iki/inventory.Tube")
impact.to_pandas()  # hrid, title, type, project, depth, archived, path
impact.summary()["total"]  # in every page; "hidden" counts trails you can't read
iki.entities.downstream_csv("TUB-000001")  # everything at once, as the CSV has it
iki.entities.graph("PRT-000123", up=2, down=1)  # a few steps each way, to draw
# In a search, with any other condition: every sample from the donor not yet released.
from_donor = {"field": "downstream", "op": "of", "value": "TUB-000001"}
unreleased = {"not": {"field": "state", "op": "eq", "value": "released"}}
iki.entities.search("iki/samples.Sample", where={"all": [from_donor, unreleased]})

# What plugins' rules caught (ADR 0127): what a rule holds for review, and what breaks a
# rule it was saved before, by the rule's name, or by any with is_set. Each says why.
held = {"field": "held", "op": "eq", "value": "purity_in_specification"}
breaking = {"field": "breaks", "op": "is_set", "value": True}
caught = iki.entities.search("iki/samples.Sample", where={"any": [held, breaking]})
caught.to_pandas()[["held", "breaks"]]  # the rules, by name
for record in caught:
    print(record["hrid"], [(h["label"], h["reason"]) for h in record["holds"]])
```

Conditions take `op` as one of `eq`, `ne`, `in`, `lt`, `lte`, `gt`, `gte`,
`contains`, `starts_with`, `matches`, `has`, `of`, `motif` and `is_set`, and combine with
`{"all": [...]}`, `{"any": [...]}` and `{"not": ...}`.

A sequence field takes `motif` (ADR 0175): bases or residues in IUPAC codes, found on a
duplex's other strand too, and across a circular one's origin:

```python
# EcoRI's site; the M13/pUC reverse primer's, on either strand of a duplex.
for site in ("GAATTC", "CAGGAAACAGCTATGAC"):
    motif = {"field": "bases", "op": "motif", "value": site}
    iki.entities.search("iki/dna.DNASequence", where=motif)
# N, any residue, S or T: N-glycosylation sequons, in IUPAC codes for proteins.
sequon = {"any": [{"field": "sequence", "op": "motif", "value": v} for v in ("NXS", "NXT")]}
iki.entities.search("iki/protein.Protein", where=sequon)
```

A search's results also download as CSV, for a spreadsheet:
`GET /api/v1/entities/search.csv` takes the same search as parameters (`where` as
JSON), and writes the grid's columns, as you may read them, a page at a time. Each
download is in the audit log, and no cell is one a spreadsheet would run as a formula
(ADR 0175). The grid's Download CSV link is this download.

## Running actions

```python
iki.actions.list(takes=["iki/protein.Protein"])  # what can run on proteins
iki.actions.get("iki/protein.compute_properties")  # its inputs, params, tier

run = iki.run(
    "iki/protein.compute_properties",
    project="kinase-atlas",
    inputs={"protein": "PRT-000123"},  # by HRID, ID, or the entity itself
    params={"ph": 7.4},
    reason="Weights for the summary table",
)
run = iki.wait(run)  # raises iki.RunFailed if it fails
print(run["effects"])  # what it created or changed

# One run for each, all or none.
runs = iki.run_each(
    "iki/protein.compute_properties",
    [p for p in found],
    each="protein",
    project="kinase-atlas",
    reason="The whole set",
)
iki.wait(runs, timeout=3600)  # iki.TimedOut after an hour

print(iki.runs.log(run))  # what it printed
iki.runs.list(project="kinase-atlas", status="failed").to_pandas()
iki.runs.cancel("RUN-000045", reason="Wrong pH")
```

An action that sends data to an outside API, such as a hosted model, says
what it sends and where in its `apis`. Starting it needs your agreement, as
`consent`, and an Org Admin's approval of the API for the project
([ADR 0130](../../docs/adr/0130-outside-apis-are-approved-per-project-and-called-through-the-proxy.md)):

```python
try:
    run = iki.run(
        "iki/example_models.predict_structure",
        project="kinase-atlas",
        inputs={"protein": "PRT-000123"},
    )
except iki.ConsentRequired as refused:
    for statement in refused.statements:
        print(statement["statement"])  # "This sends your data to FoldCloud (...)..."
    run = iki.run(
        "iki/example_models.predict_structure",
        project="kinase-atlas",
        inputs={"protein": "PRT-000123"},
        consent=refused.apis,
    )
except iki.NotApproved as refused:
    print(refused.detail, refused.approvers)  # who can approve it, in Iki's web app

for call in iki.runs.api_calls(run):  # the model and version that answered, and hashes
    print(call["model"], call["version"], call["response_sha256"])
```

## Files

Upload a path to a project where you can edit. The SDK computes the file's
SHA-256, sends each part directly to object storage, and waits for Iki to
verify the stored bytes. It returns a `FileOut` with the file's ID, name,
size, media type and SHA-256.

```python
project = "kinase-atlas"
coordinates = iki.files.upload("abl.pdb", project=project, media_type="chemical/x-pdb")

# An entity's file field takes the ID.
structure = iki.entities.create(
    "iki/protein.ProteinStructure", {"structure": coordinates["id"]}, project=project
)

# An import action takes the ID in its file parameter.
run = iki.run("iki/protein.import_structure", project=project, params={"file": coordinates["id"]})
```

You can also pass an open binary file or stream. The SDK reads from its
current position and leaves it open. It restores the position of a seekable
stream and uses a temporary file for a stream it can read only once.
Give an unnamed stream a filename with `name=`. For scientific formats,
set `media_type=` to a type the field or action accepts.

Uploading requires project Editor access and the `write` scope. A reader
who tries to upload gets `iki.Forbidden`. If Iki rejects the bytes during
verification, `iki.IkiError` includes its reason. Pass `timeout=` in
seconds to raise `iki.TimedOut` if verification takes longer.

```python
entity = iki.entities.get("STR-000007")
for file_id, file in entity["files"].items():
    iki.files.download(file_id, ".")  # checked against the checksum Iki recorded
```

## Importing spreadsheets

Import an XLSX workbook or a CSV file into a type's records in one call. Iki maps
its columns to the type's fields by their headers (or by a mapping the project
saved for sheets like it), checks every row as it would be written, then writes
them all in one run, or none of them. Each record's origin names the workbook,
its sheet and its row.

```python
imported = iki.spreadsheets.import_sheet(
    "manifest.xlsx",
    type="acme/protein.Sample",
    project="kinase-atlas",
    locale={"decimal": "comma", "dates": "day_first"},
    reason="Batch 12 from the core facility",
)
samples = iki.entities.search("acme/protein.Sample", run=imported["run"]["hrid"])
```

Iki never guesses how a sheet writes numbers and dates. Until `locale` says,
a cell that reads two ways, such as `1,234` or `03/04/2026`, is a problem.
Rows with problems raise `iki.ImportFailed`, whose `problems` give each one's
row, column and what's wrong. Pass `skip_problems=True` to import the other rows.

To look before importing, take it a step at a time:

```python
sheet = iki.spreadsheets.start("manifest.xlsx", type="acme/protein.Sample", project=project)
sheet["columns"]  # each column's header, the field Iki suggests, a few of its cells
sheet["readings"]  # how its numbers and dates read, cell by cell
sheet = iki.spreadsheets.change(sheet, columns=[{"index": 0, "field": "label"}, ...])
sheet = iki.spreadsheets.preview(sheet)
sheet["preview"]["counts"]  # rows to create, register, add, reuse, merge, or fix
for row in iki.spreadsheets.rows(sheet, outcome=["problem"]):
    print(row["row"], row["problems"])
imported = iki.spreadsheets.commit(sheet, reason="Batch 12")
template = iki.spreadsheets.save_mapping(imported, "Core facility manifest")
```

Pass the import returned by each step to the next one. Every new preview gets
its own import revision, even if the sheet and mapping stayed the same. Reading
rows with `rows(sheet)` keeps every page on that preview. If someone changes it
before you finish reviewing or committing it, the SDK raises `iki.Conflict`.
Read the current import, review its preview and decide whether to commit it.

To replace a saved template, pass both the import you reviewed and the template
you intend to replace. For example, after reviewing the entries returned by
`iki.spreadsheets.mappings(project)`, call
`iki.spreadsheets.save_mapping(sheet, template["name"], replace=template)`.
The server checks the import revision and the template revision together.
Saving a mapping on an open import can change its recorded mapping source.
Read that import again before previewing it.

Importing requires project Editor access and the `write` scope. In the REST
API, start with `POST /projects/{id}/spreadsheet-imports`, then send `PATCH`
and `POST …/preview` on `/spreadsheet-imports/{id}` with the import's
`If-Match` precondition. Use the new ETag returned by the preview for
`GET …/rows` and `POST …/commit`. To replace a template, send the source
import's ETag in `If-Match` on `POST …/mappings` and include `name`,
`replace` (the template ID) and `target_revision` in the body. HTTP 412
identifies a changed import. HTTP 409 with `stale_template` identifies a
changed template. Both refusals leave the saved mapping untouched.

## Errors

When Iki refuses something, you get an `iki.ApiError` that says why, with a
`code` for programs, `errors` for invalid input, and a `request_id` to quote to
your administrator. Its subclasses let you catch what you'd handle differently:

| Exception         | When                                                          |
| ----------------- | ------------------------------------------------------------- |
| `iki.Invalid`     | What you sent isn't valid; `errors` says what, field by field |
| `iki.NotFound`    | There's no such thing, or you can't see it                    |
| `iki.Forbidden`   | Your role, or your sign-in's scopes, don't allow it           |
| `iki.Conflict`    | It can't be done now, or it changed since your revision       |
| `iki.RateLimited` | Too many requests, even after waiting as Iki asked            |
| `iki.NotSignedIn` | No sign-in, or it has expired: run `iki.login()`              |
| `iki.RunFailed`   | A run you waited for didn't succeed                           |
| `iki.TimedOut`    | The wait expired while the run or file check was in progress  |

Requests that Iki asks you to slow down for are sent again after waiting as
long as it says.

## Types

Answers are the API's JSON, as plain dicts and lists. `iki.schema` has a
`TypedDict` for each, generated from Iki's OpenAPI description, so editors
complete keys and type checkers check them:

```python
from iki.schema import EntityOut, RunOut
```

## More than one deployment

```python
staging = iki.Client("https://iki-staging.example.com")
staging.entities.search("iki/dna.DNASequence").first()
```

`iki.Client(url, token=...)` takes a token directly, too. For anything the SDK
doesn't wrap, `client.request("GET", "/entity-types")` calls the API itself.
