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

proteins = iki.entities.search("protein.Protein", project="kinase-atlas")
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
    "protein.Protein",
    project="kinase-atlas",
    q="kinase",
    where={
        "all": [
            {"field": "length", "op": "gte", "value": 300},
            {"field": "organism", "op": "eq", "value": "Homo sapiens"},
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
    "protein.Protein",
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

iki.entities.history("PRT-000123")  # who changed what, when, how and why
iki.entities.provenance("PRT-000123")  # where each value came from
iki.entities.origins("DNA-000042")  # the database record or file it was imported from
iki.entities.lineage("PRT-000123", up=2, down=1)
```

Conditions take `op` as one of `eq`, `ne`, `in`, `lt`, `lte`, `gt`, `gte`,
`contains`, `starts_with`, `matches`, `has` and `is_set`, and combine with
`{"all": [...]}`, `{"any": [...]}` and `{"not": ...}`.

## Running actions

```python
iki.actions.list(takes=["protein.Protein"])  # what can run on proteins
iki.actions.get("protein.compute_properties")  # its inputs, params, tier

run = iki.run(
    "protein.compute_properties",
    project="kinase-atlas",
    inputs={"protein": "PRT-000123"},  # by HRID, ID, or the entity itself
    params={"ph": 7.4},
    reason="Weights for the summary table",
)
run = iki.wait(run)  # raises iki.RunFailed if it fails
print(run["effects"])  # what it created or changed

# One run for each, all or none.
runs = iki.run_each(
    "protein.compute_properties",
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

## Files

```python
entity = iki.entities.get("STR-000007")
for file_id, file in entity["files"].items():
    iki.files.download(file_id, ".")  # checked against the checksum Iki recorded
```

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
| `iki.TimedOut`    | Waiting took longer than you allowed; the run goes on         |

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
staging.entities.search("dna.DNASequence").first()
```

`iki.Client(url, token=...)` takes a token directly, too. For anything the SDK
doesn't wrap, `client.request("GET", "/entity-types")` calls the API itself.
