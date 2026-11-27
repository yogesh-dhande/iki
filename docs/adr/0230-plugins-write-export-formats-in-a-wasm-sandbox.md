# 0230. Plugins write export formats in a Wasm sandbox that holds only the requester's data

- Status: Accepted
- Date: 2026-11-25
- Refines: [0171](0171-a-types-records-export-as-files-iki-writes-from-its-fields.md),
  [0213](0213-plugins-declare-their-file-formats.md),
  [0034](0034-deployment-lockfile-and-plugin-builds.md),
  [0017](0017-supply-chain-security.md)

## Context

ADR 0171 made a record's download a file Iki writes from its values at a
revision. The same revision always gives the same file, the download is
audited with the file's SHA-256, and what the format couldn't hold is listed.
ADR 0229 extended this to many records as one file. Only Iki's own writers
do either. A format only a plugin knows can still only be written by an action
on a worker. That needs a Runner and a wait, reads only the latest revision,
and leaves an artifact that expires. Downloading that artifact isn't audited.
ADR 0229 removed the small_molecule plugin's SDF action for this reason, once
Iki wrote SDF itself.

Labs need files Iki won't write itself. A liquid handler needs a pick list. A
sequencing core needs a sample sheet, a vendor needs an order form, and a
partner needs their LIMS's import layout. Each comes from one record or from
many: a plate's wells, or a selection of samples.

Writing them on the server means running plugin code there, which Iki has
avoided so far:

- Actions run on workers, in isolated pods (ADR 0047).
- Components run in sandboxed frames in the browser (ADR 0084).
- Formats declare readers and viewers by name, and Iki supplies them. "A
  manifest cannot supply executable code to the upload service or the
  browser this way" (ADR 0213).
- Plugin migrations' `RunPython` runs once, at deploy, in the migrate Job.
  It is reviewed with the plugin, and it can import only what the server
  image already has.

A writer is different. It runs on every download, on values people can
edit. It brings its own dependencies, so a compromised crate is a realistic
way in, not only a malicious author. The server process holds the database
credentials, Django's secret key, the electronic-signature keys (ADR 0126)
and the cloud identity that reads the files buckets. Code that escapes into
that process can forge records, signatures and the audit log.

WebAssembly gives a sandbox by construction: a module touches only its own
memory and the functions it imports. Iki already runs its own module,
iki-wasm, inside the server under wasmtime, with no imports and a fuel
budget, for rules and expressions (ADR 0127). That module is Iki's own code,
built from Iki's repository. A plugin's module is not. And a runtime's
sandbox isn't perfect. In October 2026 Wasmtime published two Critical
advisories, both sandbox escapes through Cranelift miscompiling a crafted
module: an overflow on very large function bodies
([GHSA-897f-g5gm-fmxm](https://github.com/bytecodealliance/wasmtime/security/advisories/GHSA-897f-g5gm-fmxm))
and a wrong jump on huge `br_table`s on aarch64
([GHSA-mghf-wjfp-p5xm](https://github.com/bytecodealliance/wasmtime/security/advisories/GHSA-mghf-wjfp-p5xm)).
In November it published a guest-triggered host abort
([GHSA-43j2-gcp4-m22g](https://github.com/bytecodealliance/wasmtime/security/advisories/GHSA-43j2-gcp4-m22g),
which needs the GC proposal and a host function returning GC objects). Each
one was triggered by the module, not by the data it was given.

## Decision

**A plugin declares writers. Each writer is a WebAssembly module built from
the plugin's source and pinned by its digest. Iki runs a writer under
wasmtime, with no imports, fuel and a memory cap, in a sandbox service of
its own: a pod with no credentials, no network and no cloud identity. Each
export runs in a fresh process there, which holds only the records that
export's requester may read.** If a module escapes Wasm, all it can reach is
data the requester could already read, and all it can do is spoil that
requester's file. Iki audits that file by its hash, as it audits its own
writers' files.

Accepted with these decisions:

1. **A separate sandbox Deployment.** It has no Secrets, no service-account
   token, no network egress and no cloud identity. Each export gets a fresh
   process holding only the records the requester may read. Writers run there
   under wasmtime-py with no imports, deterministic fuel, a memory cap, a small
   stack, a wall-clock kill, and the build-time caps on function body size and
   `br_table` targets. wasmtime-py is pinned at 51.0.0. It's still the latest
   release, published on 2026-11-20.
2. **Writers are Rust only, for now.** Python writers may come later. The ABI
   is about the module, not the language, so they don't need it changed.
3. **The `iki-writer` SDK crate lives in Iki's repository**
   (`worker/crates/iki-writer`). It is published to crates.io with each
   release. Until then the builder supplies it from the Iki that builds the
   plugin, as a path patch. Claiming the crate's name is a release task.
4. **Offline sites** can vendor their crates in the plugin repository, or
   point the build at a registry mirror. Both are supported, and both give the
   same module.
5. **A failed export is logged and counted, not audited as a download,**
   because no file left Iki. Asking for many records is still audited when
   it's asked (`entities.export_requested`), as in ADR 0229.
6. **No plugin action keeps writing SDF.** The draft assumed
   `small_molecule.export_sdf` would stay for coordinates RDKit computes, but
   ADR 0229 removed it.

### What a plugin declares

A writer goes under `writers` in `plugin.yaml`. Its ID is qualified like a
format's: `examples/liquid_handling.tecan-worklist`. That is the example plugin
(`examples/plugins/liquid_handling`), which writes transfers between plates as
a Tecan worklist (`.gwl`) for Freedom EVOware and FluentControl. Liquid
handlers commonly run cherry-picks from these files, and no format Iki ships
covers them.

> Note, 2026-11-26: the example now uses the `acme/` placeholder namespace like
> every other example, so its writer is `acme/liquid_handling.tecan-worklist`.

```yaml
formats:
  tecan-gwl:
    label: Tecan worklist
    media_types: [text/x-tecan-gwl]
    extensions: [.gwl]
writers:
  tecan-worklist:
    label: Tecan worklist
    description: An aspirate, a dispense and a tip wash for each transfer, in order.
    format: tecan-gwl # what the file is: its media type and extension (ADR 0213)
    module: writers/tecan-worklist # a Rust crate in the plugin, with its Cargo.lock
    many: file # file | zip | one
    memory: 16MiB # at most 256MiB; 64MiB unless set
    roles:
      source_plate: { type: ref, types: [Plate], required: true }
      source_well: { type: string, required: true }
      destination_plate: { type: ref, types: [Plate], required: true }
      destination_well: { type: string, required: true }
      volume: { type: quantity, unit: uL, required: true }
      liquid_class: { type: string }
```

A type exports through a writer by mapping the writer's roles to its own
fields, as a type names fields for Iki's own exports (ADR 0171):

```yaml
exports:
  - export: tecan-worklist
    fields:
      source_plate: source
      source_well: source_well
      destination_plate: destination
      destination_well: destination_well
      volume: volume
      liquid_class: liquid_class
```

- **The format says what the file is.** It may be the plugin's own format or
  another plugin's. Using another plugin's needs that plugin under
  `dependencies`, as for fields (ADR 0213). The format supplies the media type
  and the extension, so a CSV layout doesn't claim `.csv` against core.
  Validation refuses formats that can carry active content: `text/html`,
  `application/xhtml+xml`, `image/svg+xml`, XSLT, script types and
  `application/wasm`. The server refuses them again at runtime.
- **Roles take field types.** Validation checks that each mapped field exists
  and is of its role's type, and that every required role is mapped. A
  quantity role names a unit. The field it reads must measure the same thing,
  and the writer gets the value converted to the role's unit. A text role also
  reads a string field, and a float role an int one. A
  subtype inherits its ancestor's exports, as in ADR 0171. A writer sees only
  the roles its type maps, so a reviewer can see which data goes into the
  file.
- **`many` says how many records a file holds.** `file`: one file for any
  number of records, written by the module. `zip`: one file per record, and
  Iki puts them in a ZIP. `one`: a single record only.
- **The module is source.** `writers/echo-picklist/` is a Rust crate with
  `Cargo.toml` and `Cargo.lock`. Beside it is
  `writers/echo-picklist.wasm.sha256`, the SHA-256 of the module built for
  Linux x86-64. No `.wasm` is ever tracked in git. The crate depends on
  `iki-writer`, Iki's SDK crate, which gives the ABI below as a trait and
  typed inputs. A crate builds from its own directory alone, plus its
  registry dependencies. The builder copies the crate, with the SDK inside it,
  so Cargo's hash of the SDK's path is the same wherever the plugin is. No
  build script or `include_bytes!` can reach the plugin's other files. Symbolic
  links are refused.

### How the module is built, pinned and reviewed

- **One builder.** The builder is the Rust image Iki builds its own modules
  with (`rust:1.93.0-slim-trixie`), pinned by digest. It is the same stages in
  `deploy/plugins.Dockerfile` and in `iki plugin writers --update`, and a test
  keeps them word for word the same (`iki_spec.writers.build`). It always runs
  as `linux/amd64`, because that is the platform the digest is of. A module built on macOS arm64 doesn't match, and isn't expected to.
  rustc links its own precompiled wasm32 standard library, which differs by
  host (ADR 0200).
- **The author writes the digest.** `iki plugin writers --update` builds each
  writer in the builder, through Docker, and writes its `.wasm.sha256`. A
  change to the Rust that changes the module changes the digest, and a
  reviewer sees both in the same diff.
- **The deployment builds from source and compares.** `iki deploy build`
  stages each writer's crate and digest, and the SDK, under
  `iki_installed_plugins/writers`. The plugin image build
  (`deploy/plugins.Dockerfile`) rebuilds each one from that source. Fetching
  the crates comes first, from crates.io, the site's registry mirror
  (`IKI_CARGO_MIRROR`), or the plugin's vendored crates
  (`cargo vendor --versioned-dirs`). Cargo checks every crate against the
  checksum in `Cargo.lock`. Then the build runs with the network off
  (`RUN --network=none`), with no credentials, and with paths remapped: the
  crate to `/writer` and registry or vendored crates to `/crates`. So a build
  script or proc macro runs where it can't reach anything, and a mirror or
  vendored build gives the same bytes as crates.io. If the module's SHA-256
  doesn't match the tracked digest, the build fails and names the writer and
  both digests. The image carries the checked modules, not the writers'
  sources.
- **The checker refuses what can't be a writer.** It runs on every built
  module in the writers stage, and again in `iki plugin validate`. It
  refuses:
  - any import;
  - an ABI export that is missing or has the wrong type;
  - more than one memory, a shared memory or a 64-bit memory;
  - a module over 16 MiB, a function body over 1 MiB, or a `br_table` with
    more than 65,536 targets.

  The last two limits shut out the inputs behind both Critical advisories
  above. They don't fix compiler bugs in general. Keeping wasmtime patched
  does that.

- **The image carries native code that was checked.** The stage precompiles
  each module with the server image's wasmtime-py and the sandbox's engine
  settings. It writes the module, its `.cwasm` and a manifest of both digests
  under a root-owned, read-only directory. The sandbox loads them with the
  checks `iki_spec.wasm` already applies to iki-wasm's precompiled code: root
  ownership, the digests, the target and the wasmtime version. If any check
  fails, it compiles the verified `.wasm` instead.
- **The lock records it.** `iki_installed_plugins/lock.json` lists each
  writer's module digest beside its plugin's commit. Each export's audit
  event names the module that wrote the file.
- **People review the source.** Deployed plugins are trusted and reviewed by
  the enterprise (ADR 0006). A writer's review covers its Rust and its
  `Cargo.lock`. `iki plugin writers` lists each writer's digest and the
  crates its `Cargo.lock` pins, with their versions. Licences and advisories
  come from the lab's own dependency scanning of that lock, as for any Rust
  project. A first-party writer would go through the same path, its digest
  file part of the plugin tree that `first_party_trees.json` pins for mirrors
  (ADR 0114). None ships yet.

### The ABI

A writer is a core WebAssembly module, ABI 1. It has no imports. It exports
`memory` and these functions, all `i32`:

| export                             | what it does                                                                                                   |
| ---------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `iki_abi() -> i32`                 | Returns 1.                                                                                                     |
| `iki_input(len) -> ptr`            | A buffer of `len` bytes for the next request. Iki writes canonical JSON into it.                               |
| `iki_begin() -> status`            | Starts a file. The request is the export: `{abi, writer, format, records}`.                                    |
| `iki_record() -> status`           | Writes one record. The request is the record, as below.                                                        |
| `iki_end() -> status`              | Finishes the file.                                                                                             |
| `iki_output()`, `iki_output_len()` | The bytes this call adds to the file.                                                                          |
| `iki_notes()`, `iki_notes_len()`   | This call's notes as JSON: `[{"code", "message", "role"?}]`. On a refusal, `[{"code": "refused", "message"}]`. |

Status 0 means written. Status 1 means refused: the writer can't write this
record in this format, and its note says why.

A record's request is:

```json
{
  "id": "…",
  "hrid": "PLT-000042",
  "name": "Assay plate 42",
  "type": "acme/plates.Transfer",
  "revision": 3,
  "latest": true,
  "revised_at": "2026-11-20T09:14:03Z",
  "roles": { "volume": { "value": 2.5, "unit": "nL" }, "source_plate": { "…": "…" } },
  "withheld": []
}
```

- **Values are given as the API gives them,** at the record's revision.
- **A reference role holds the referenced record's fields, one step deep.**
  The referenced record is read as it was while this revision was the
  record's latest, as ADR 0171 reads a plasmid's sequence.
- **A reference the requester can't read is left out and named in
  `withheld`.** Iki adds its own note for it, so the file is never silently
  short. A writer that can't do without it refuses the record, as the Tecan
  writer does.
- **A quantity is `{value, unit}`, in the role's unit.**
- **The JSON is canonical:** keys sorted, no insignificant whitespace, UTF-8.
- **There is no clock, no randomness, no requester and no export time.**
  The file depends only on the records at their revisions, their order and
  the module. Within ABI 1, the request for a given revision never changes.
  A new field in the request is ABI 2, and a module says which ABI it reads.

The engine runs with:

- fuel;
- NaN canonicalisation;
- copy-on-write memory initialisation off, so instantiation costs the same
  fuel on every host;
- threads, relaxed SIMD, GC, the component model, exceptions, stack
  switching, multi-memory and 64-bit memory off;
- a 512 KiB Wasm stack.

These follow Wasmtime's
[deterministic execution guide](https://docs.wasmtime.dev/examples-deterministic-wasm-execution.html).
Turning off proposals a writer doesn't need also turns off the code paths
behind most of this year's advisories.

### Limits

| limit                      | default                                | where it's enforced                    |
| -------------------------- | -------------------------------------- | -------------------------------------- |
| imports                    | none                                   | checker; instantiation with no imports |
| module size                | 16 MiB                                 | checker                                |
| linear memory              | the writer's `memory`, at most 256 MiB | store limits (`memory.grow` fails)     |
| fuel per call              | 1,000,000,000 + 2,000 per request byte | wasmtime fuel                          |
| output per call            | 64 MiB                                 | sandbox                                |
| notes                      | 1,000 per record, 500 characters each  | sandbox                                |
| request per call           | 64 MiB                                 | sandbox, before it's written           |
| wall clock, a waiting file | 120 s                                  | sandbox kills the process              |
| wall clock, in background  | 30 minutes                             | sandbox kills the process              |
| concurrent exports per pod | 4                                      | `busy`, then 503 with `Retry-After`    |

A deployment may lower any of these, and may raise fuel and the wall clock.
The chart's `sandbox.limits` sets them (2026-11-27): the server reads them as
`IKI_EXPORT_*` settings and sends any that differ from Iki's in each
export's header, the child refuses a header that raises a limit other than
fuel, and the launcher's `--max-deadline` is the longest deadline the server
gives.
A plugin may ask only for less memory than the cap. Fuel is deterministic:
the same call runs out at the same instruction on every host. The wall
clock is a backstop for the host side, and it can only fail an export. It
never changes the bytes.

### The sandbox service

The chart gets a `sandbox` component, enabled when the build has any
writers.

- **The pod.** It runs the deployment's plugin image, with no Secrets mounted.
  Its ServiceAccount has no permissions, no token
  (`automountServiceAccountToken: false`) and no cloud identity binding (no
  Workload Identity, no IRSA). It runs as non-root, with a read-only root
  filesystem, all capabilities dropped and seccomp `RuntimeDefault`.
- **The network.** The chart's default-deny NetworkPolicy covers it, and the
  rule that gives other Iki pods DNS and the database leaves it out. So it
  has no egress at all. Only web and tasks pods may connect to it.
- **The launcher.** `python -I -m iki_spec.writer_sandbox` re-executes
  itself with an empty environment, whatever the image set. It accepts each
  export as a stream of length-prefixed frames: a header naming the writer,
  its module's digest, its memory and a deadline, then one frame per call.
  It hands the stream to a child process and relays the child's answers
  back: the bytes, the notes, the fuel used, or why it failed. It reads only
  the header, never a record. Past its concurrency it answers `busy`. At an
  export's deadline it kills the child and says so. On SIGTERM it takes no
  more exports and lets those running finish. Its readiness probe passes
  only once a child has loaded and checked every installed writer, so an
  image with a bad module doesn't roll out.
- **The child.** Each child is a fresh `python -I` process with an empty
  environment and only its pipes open. It loads the precompiled module,
  instantiates it in a new store with its limits, then gives up what a writer
  never needs: it can't create a file, open a descriptor or start a process
  (rlimits). It runs `begin`, one `record` per record and `end`, then exits.
  Each `begin` starts from a fresh instance, so a ZIP's files share nothing.
  The launcher keeps two children started and waiting, so the one-record path
  doesn't pay for the start. No child is ever used twice.
- **The server's side.** Web and task pods get `IKI_EXPORT_SANDBOX`, the
  sandbox's Service address (`tcp://host:port`). They take the module's
  digest from the image's manifest and check that it is the one the plugin
  pins.
- **Development and tests.** The server can start the child itself
  (`IKI_EXPORT_SANDBOX=local`). The server refuses that setting unless local
  plugins are allowed, as only a development build allows them (ADR 0034).

wasmtime-py is the runtime, the binding the server already pins. It is
licensed Apache-2.0 WITH LLVM-exception, ships `py3-none` wheels for Linux
x86-64 and arm64, and supports Python 3.9 to 3.14. The current release is
51.0.0, published on 2026-11-20. The server pinned 50.0.1, which ships the
50.0.0 engine. That engine is in the range of GHSA-43j2-gcp4-m22g, which
needs the GC proposal. iki-wasm runs with no host functions, so it wasn't
exposed. The sandbox's engine has GC off. The pin moves to 51.0.0 with this
ADR, and with it the server's precompiled iki-wasm code. Dependabot keeps it
moving (ADR 0017), and a wasmtime advisory is treated as a runtime bump.

### One record and many

The 62j export path drives every writer through one interface:

```
begin(export) -> part
record(record at revision) -> part, notes
end() -> part, notes
```

Iki's own writers implement it in the server process. A FASTA writer's
`begin` and `end` write nothing. A plugin's writer implements it by
streaming the same calls to one sandbox child. 62j then treats both alike:
which records, which revisions and in what order, the container, the
audit and the size limits.

- **One record** is `begin`, one `record` and `end`. It's served as ADR 0171
  serves a file: `GET /entities/{ref}/export?format=acme/liquid_handling.echo-picklist&revision=N`.
  The file is named by Iki, never by the writer.
- **Many records with `many: file`** are one child, `begin`, a `record` per
  record in 62j's order, then `end`. The records all belong to one requester,
  so one process may hold them all.
- **Many records with `many: zip`** are `begin`/`record`/`end` per record,
  in the same child, each file from a fresh instance. Iki writes the ZIP
  deterministically: entries in order, each named as its record's download
  would be, each dated by its revision, stored uncompressed.
- **`many: one`** isn't offered for a selection.

**Nothing reaches the requester before `end` succeeds.** The server spools
the parts, hashes them as they arrive, and serves the file only when the
whole file is written. If the format declares byte signatures (ADR 0213),
the file must start with one, or it isn't served. In a ZIP, each record's file
must.

**Every export is audited as Iki's own are.** `entity.exported`, and 62j's
many-record event, gain
`writer: {plugin, writer, commit, module_sha256, abi}` beside the file's name,
size, SHA-256 and the notes per record. An auditor can therefore tell which
code wrote the file someone took. The response sets `Iki-Left-Out`, as in
ADR 0171. The export dialog lists a plugin's formats with their
descriptions and says which plugin writes them. When the format has no
place for notes, the dialog shows them.

### When it fails

No partial file is ever served.

- **A refusal** (status 1) is a 422 `writer_refused` that names the record
  and quotes the writer's message as text. For many records, the export is
  refused, not shortened, and the person can drop that record.
- **A trap is a 422 `writer_failed`.** That covers running out of fuel,
  failing to grow memory, a stack overflow, `unreachable` (a Rust panic),
  going over an output or notes limit, malformed notes, a wrong ABI and the
  deadline. The error says the plugin's writer failed, and on which record.
  The log has the plugin, the writer, the module digest, the trap and the
  record ID. The values don't go in the log.
- **A sandbox that's down or full** is a 503 `sandbox_unavailable` with
  `Retry-After`. Iki's own formats keep working. An export of many records
  written in the background tries again, four times 30 seconds apart, before
  it fails.
- **A failed export isn't audited,** because no file left Iki. It is logged
  and counted.
- **A module that fails its digest or checks at load is unavailable.** The
  sandbox refuses it, so its exports are 503 rather than a file. Its
  readiness reports the problem, so a bad image doesn't roll out.

## Consequences

- A plugin's format downloads from the record page, or for a selection, at
  any revision, with no Runner, worker or wait. The audit names the module
  that wrote it.
- The same records at the same revisions, in the same order, with the same
  module digest, give the same bytes on any host. Fuel exhaustion is just as
  repeatable. The spike measured this (below).
- Plugin code with dependencies still never runs where Iki's credentials
  are. A Wasm escape gains a process with nothing in it but the requester's
  own records, and no way out.
- A deployment with writers runs one more small Deployment. Its pods need
  memory for 4 × (the largest writer's `memory` + about 50 MB). The 50 MB is
  what a child measured after importing wasmtime (46 MB on macOS arm64). The
  chart's default limit, 768 MiB, fits four 64 MiB writers. Its values say
  how to size it for larger ones.
- A one-record export pays a network hop and a frame or two on top of the
  writer. The process start, about 80 to 160 ms natively, is hidden by the
  child that's already waiting.
- Writer crates pin `iki-writer = "=0.1.0-dev.0"`, so a release that bumps
  the SDK's version bumps each writer's `Cargo.toml` and `Cargo.lock` too.
- Writers are Rust in v1, because the builder has to rebuild them and
  reproduce their digest. The ABI isn't tied to Rust. A language is added
  by adding its toolchain to the builder, and that needs a reproducible
  build there.
- Writers can't use chemistry toolkits that need a host, such as RDKit.
  RDKit's Wasm build expects JavaScript imports. A writer that needs
  computed coordinates reads them from the record, and computing them is an
  action's job. Iki's built-in SDF (ADR 0229) writes stored structures.
- Iki's own writers stay in the server process, in Python, as ADR 0171 has
  them. iki-wasm stays in the server. It's Iki's code, built from Iki's
  repository, and it runs on every save, where a hop would cost too much.
- Deployment CI needs to reach crates.io or a registry mirror to fetch a
  writer's crates. An air-gapped site points Cargo at its mirror. A plugin
  can instead vendor its crates into its repository, as source.

## Alternatives considered

- **(a) The same Wasm in the server process.** This is the simplest, the
  fastest (no hop), and how Iki runs iki-wasm. Shopify Functions and Fastly
  run third-party Wasm under wasmtime this way. Rejected for plugin modules
  for three reasons:
  - The server process holds the database, signing keys and cloud identity,
    so one compiler bug of the October kind turns a compromised crate into a
    forged audit log.
  - A guest-triggered host abort kills a web worker serving everyone.
  - It breaks the line Iki has held so far: plugin code with dependencies
    never runs with Iki's credentials.

  The spike shows a call releases the GIL, so (a) wouldn't block other
  requests. Its cost isn't speed.

- **(b′) A sidecar container in the web pod.** It has no Secrets, but it
  shares the pod's network namespace. So it could reach the database port,
  internal services and the cloud metadata endpoint that hands out the
  pod's identity. A separate pod is what keeps the network and the identity
  away.
- **(b″) The worker pool.** Workers are isolated already, but they take a
  run's lease, which takes seconds. They scale to zero, may be self-hosted on
  another network, and egress through the proxy. A one-record download would
  wait on compute meant for runs. The sandbox is small, always on and has no
  egress.
- **(c) Declarative templates only.** For example, a header and a row
  template of CEL text, which iki-wasm already evaluates (ADR 0090), with
  Iki quoting the CSV. That needs no plugin code and no sandbox, and it
  covers simple delimited layouts. It can't write a binary or zipped format
  (an XLSX order form), escape by a format's own rules, compute checksums,
  or note what didn't fit. It could later be a second writer kind beside
  this one, for labs that don't write Rust.
- **(d) Export actions on workers (the status quo).** These stay right for
  files that need a toolkit or minutes of compute. For a download, they
  need a Runner and a wait, read only the latest revision, expire, and
  aren't audited (ADR 0171).
- **Other runtimes.**
  - wasmer-python's last release was 1.1.0, in January 2022, with no wheels
    for Python 3.11 or later.
  - Extism's Python SDK (1.1.1, BSD-3-Clause) wraps a Wasm runtime and adds
    host functions and HTTP, which a writer must not have.
  - pywasm (2.2.3, MIT) is a pure-Python interpreter. It's portable, but
    it interprets each instruction in Python, and its documentation
    describes no fuel metering or memory limit.
  - The Rust `wasmtime` crate (51.0.0) would let the sandbox be a Rust
    binary, but it needs rustc 1.97 against Iki's pinned 1.93. Moving the
    toolchain moves every tracked Wasm digest. wasmtime-py uses the same
    engine and needs neither change.

## Spike

The spike, `scripts/spikes/export_writer_sandbox.py`, ran writers written in
the Wasm text format through the engine and ABI above. Its checks are now
tests (`spec/tests/test_writer_sandbox.py`), against the real runtime, child
and launcher, and the spike is gone. It ran on Python 3.12 and 3.14 with
wasmtime-py 50.0.1 and 51.0.0, on macOS arm64, and on Linux arm64 and amd64
in Docker. amd64 was emulated, so its timings are upper bounds. Every check
passed on every combination:

| check                                                 | result                                                                  |
| ----------------------------------------------------- | ----------------------------------------------------------------------- |
| a writer returns bytes and notes through the ABI      | yes; 277 fuel for begin, record and end                                 |
| same input, same module, two more processes           | same SHA-256 and the same fuel on all three platforms                   |
| an endless loop                                       | `OUT_OF_FUEL` after exactly the budget; 200M fuel took 60 to 110 ms     |
| `memory.grow` past a 16 MiB cap                       | returns -1 at exactly 16 MiB; the writer traps                          |
| a module declaring more memory than the cap           | doesn't instantiate                                                     |
| runaway recursion                                     | `STACK_OVERFLOW` trap; the host carries on                              |
| one `memory.fill` of 48 MiB                           | 50,331,656 fuel: bulk memory is charged per byte, so fuel bounds it too |
| a WASI import                                         | refused by the check; and can't be instantiated without imports         |
| another thread during a long call                     | keeps running: the call releases the GIL                                |
| a fresh `python -I` process per export, from `.cwasm` | median 77 to 158 ms natively (244 ms emulated amd64)                    |

The memory-fill result corrected an assumption in the first draft of this
ADR, that bulk memory instructions cost one unit of fuel however many bytes
they touch.
