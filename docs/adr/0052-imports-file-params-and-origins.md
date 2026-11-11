# 0052. Imports take files as params, and say where what they make came from

- Status: Accepted
- Date: 2026-11-11
- Refines: [0040](0040-action-and-environment-manifests.md), [0044](0044-run-results-are-written-all-or-nothing.md), [0046](0046-how-iki-worker-runs-a-command.md)

## Context

Most of a lab's data arrives from outside: FASTA and GenBank files from a
collaborator or a vendor, records from NCBI, UniProt and the PDB. An import
is an action with no entities to read. It needs a file, or an accession,
and makes entities. Whoever audits regulated work asks where each record
came from: which database, which version of the record, when it was
fetched, and that what was stored is what was read. ALCOA+ calls this
attributable and original data. And when an import fails, a scientist
needs to know which record, and why, not that "the command exited with
status 1".

## Decision

**A param can be a file.** `type: file` takes one file, as a file field
does, with `media_types` and `max_size`. At launch, the server checks that
the person can read it and that it fits those rules, and keeps its ID in
the run's params. The worker downloads it before the command starts, as it
does an input's files: it's in `input.json`'s `files`, checked by size and
SHA-256. A file param can't have a default. A reference can't be a param,
since entities are inputs, with their own checks. Lists of files wait until
an import needs them.

**Runs get their plugin's settings.** The values the deployment sets in its
lockfile, such as the address NCBI asks clients to give, are copied onto
the run at launch and given to the command as `input.json`'s `settings`.
A run records what it ran with, so a later change of settings can't change
what an earlier run says it did. Settings are never secrets. Development
builds (`iki plugin build`) use each setting's default.

**What a run creates can say where it came from.** In `output.json`, each
created entity may carry an `origin`:

- the `source` (such as `NCBI Nucleotide`, or `GenBank file`) and the
  record's `id` there;
- its `version` or the database's release;
- an `https` `url` people can open;
- when it was `retrieved_at`, with a time zone;
- the `sha256` of the record as the command read it;
- the `file` it was read from, which must be one of the run's file params.

The server checks the origin and keeps it with the entity, the run and the
revision it made. It also keeps one when an import finds an entity that's
already there (an identical sequence): finding the same record again is
evidence too. Origins are append-only. Readers see an entity's origins
under **Imported from** on its page, through
`GET /entities/{ref}/origins`, in MCP's `get_entity` and in the SDK. They
see the run and the file only if they can read them.

**A command can say why it failed.** A line like
`::error Record 3 (pUC19) has no bases` becomes the run's error if the
command exits non-zero, followed by how it exited. This follows GitHub
Actions' `::error::` lines. The worker keeps the last one, of at most 1,000
characters.

**The dna plugin imports FASTA and GenBank.** `dna.import_sequences` reads
either format, sniffed from the content, using only Python's standard
library. It keeps names, descriptions, accessions, organisms, topology and
features, and records each record's file and checksum as its origin. A record
that can't be read fails the whole import with a `::error` line that names
it, so nothing is half imported. Fetching from NCBI comes with per-action
network access.

## Consequences

- Imports run from the project's **New run** page, where a file param
  offers the project's files that fit, or an upload.
- An imported entity's provenance reaches back past Iki: to the database
  record and version, or to the file and record, with a checksum to check
  it against.
- Any plugin can import. The same `origin` serves file imports and
  database fetches, so the UniProt, PDB and PubChem fetches still to come
  record origins the same way.
- A failed run says why in the command's own words, on its page and
  through the API, MCP and the SDK.
