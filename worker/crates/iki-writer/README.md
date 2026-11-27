# iki-writer

Write an [Iki](https://ikisci.org) plugin's export format in Rust.

A plugin declares a writer in its `plugin.yaml`: a format, the roles a record's
fields map to, and a Rust crate in the plugin, built to a WebAssembly module.
When someone exports records in that format, Iki runs the module in a sandbox
with no imports (no clock, files or network), fuel and a memory cap, holding
only records that person can read. Iki serves the file only once the whole
file is written, and audits it by its SHA-256 and the module's digest
(ADR 0230).

This crate is the module's ABI as a trait with typed records.

## A writer

```rust
use iki_writer::{Part, Record, Refusal, Writer};

/// One line per sample: its HRID and its concentration in ng/µL.
#[derive(Default)]
struct SampleSheet;

impl Writer for SampleSheet {
    fn record(&mut self, record: &Record) -> Result<Part, Refusal> {
        let Some(concentration) = record.quantity("concentration") else {
            return Err(Refusal::new(format!("{} has no concentration.", record.hrid)));
        };
        Ok(Part::bytes(format!("{},{}\r\n", record.hrid, concentration.value)))
    }
}

iki_writer::writer!(SampleSheet);
```

```rust
pub trait Writer: Default {
    fn begin(&mut self, export: &Export) -> Result<Part, Refusal>; // writes nothing by default
    fn record(&mut self, record: &Record) -> Result<Part, Refusal>;
    fn end(&mut self) -> Result<Part, Refusal>; // writes nothing by default
}
```

- `writer!(Type)` generates the module's exports, once per crate. Its state is
  the writer, which each `begin` makes afresh from `Default`, so a file's
  totals and counters live in its fields.
- A `Part` is the bytes a call adds to the file and its notes:
  `Part::bytes(text).role_note("volume", "rounded", "…")`. A `Note` has a
  `code`, a `message` and the `role` it's about, if any. Iki lists the notes
  with the download and records them in the audit event.
- A `Refusal::new(message)` refuses the record, and so the export, and Iki
  shows the message. Refuse what the format can't hold; note what you had to
  change to fit.
- An `Export` has the `abi`, the `writer`'s and the `format`'s qualified IDs,
  and how many `records` the file will hold.
- A `Record` has its `id`, `hrid`, `name`, `r#type`, `revision` and
  `revised_at`, its `roles` and the `withheld` roles. Read roles with
  `text`, `integer`, `number` (integer or float), `boolean`, `quantity`
  (`Quantity { value, unit }`, already in the role's declared unit),
  `reference` and `value`, each an `Option`, and `is_withheld`. A
  `Reference` is the referenced record, one step deep, with the same
  accessors over its `fields`.

A role with no value, or one the type doesn't map, is absent. A reference the
person exporting can't read is absent too, and its role is in `withheld`.

## The ABI

ABI 1, a core WebAssembly module with no imports. It exports `memory` and these
functions, each argument and result a wasm `i32`:

| export                             | what it does                                               |
| ---------------------------------- | ---------------------------------------------------------- |
| `iki_abi() -> i32`                 | 1.                                                         |
| `iki_input(len) -> ptr`            | A fresh buffer of `len` bytes, for the next request.       |
| `iki_begin() -> status`            | Starts a file. The request is the export.                  |
| `iki_record() -> status`           | Writes one record. The request is the record.              |
| `iki_end() -> status`              | Finishes the file. The request is `{}`.                    |
| `iki_output()`, `iki_output_len()` | The bytes this call adds to the file.                      |
| `iki_notes()`, `iki_notes_len()`   | This call's notes: `[{"code","message","role"}]`, as JSON. |

Iki writes each request into the buffer `iki_input` gives, as canonical JSON
(keys sorted, no insignificant whitespace, UTF-8, at most 64 MiB), then makes
the call. Status 0 is written, 1 refused. A note's `role` is left out when it
isn't about one; a refusal's notes are exactly
`[{"code":"refused","message":"…"}]`, with no bytes. A request that isn't one
is Iki's bug, so the module panics: a trap, under `panic = "abort"`.

```json
{
  "abi": 1,
  "format": "acme/liquid_handling.gwl",
  "records": 96,
  "writer": "acme/liquid_handling.tecan-worklist"
}
```

```json
{
  "hrid": "TRF-000042",
  "id": "6f1c1b7e-2a6d-4c1e-9b0a-3f6f5f9b2d41",
  "name": "Dose-response, compound 12",
  "revised_at": "2026-11-20T09:14:03Z",
  "revision": 3,
  "roles": {
    "source_plate": {
      "fields": { "barcode": "CP-0412", "label": "Compound source 1", "wells": "plate_384" },
      "hrid": "PLT-000412",
      "id": "2f0c9a43-61b8-4a1e-8d2e-0c9b0a7c5e10",
      "name": "Compound source 1",
      "revision": 7,
      "type": "acme/liquid_handling.Plate"
    },
    "source_well": "A1",
    "volume": { "unit": "uL", "value": 2.5 }
  },
  "type": "acme/liquid_handling.Transfer",
  "withheld": ["destination_plate"]
}
```

## The crate

A writer lives in its plugin at `writers/<name>/`, beside
`writers/<name>.wasm.sha256`, the SHA-256 of its module built for Linux
x86-64. No `.wasm` is tracked in git.

```toml
[package]
name = "tecan-worklist"
version = "0.1.0"
edition = "2024"
publish = false

[workspace] # its own, so no other workspace takes it

[lib]
crate-type = ["cdylib", "rlib"] # the module, and native unit tests

[dependencies]
iki-writer = "=0.1.0-dev.0"

[profile.release]
lto = true
codegen-units = 1
panic = "abort"
strip = "symbols"
opt-level = "s"
```

It builds from its own directory alone, with its `Cargo.lock`: no path
dependencies, and nothing read from outside it. Until this crate is on
crates.io, Cargo finds it through a patch:

```sh
PATCH='patch.crates-io.iki-writer.path="<path to worker/crates/iki-writer>"'
cargo generate-lockfile --config "$PATCH"
cargo test --config "$PATCH"
```

Unit tests run natively. Call the writer's methods, or drive it through the
code its exports run with `iki_writer::testing::Session`, which also reads
back the notes' JSON as Iki would.

## Building the module

`build-writer.sh`, beside this README, is how every build runs: the plugin
image's writers stage, `iki plugin writers --update` (which writes the
digest, from a build in that stage) and local test builds.

```sh
build-writer.sh fetch <crate dir>                 # cargo fetch --locked; the network
build-writer.sh build <crate dir> <output .wasm>  # cargo build --locked --offline
```

The build runs on a copy of the crate with this SDK inside it, with paths
remapped, so the same crate gives the same bytes wherever it, the SDK and the
cargo home are, on the same platform, and whether its crates came from
crates.io, a mirror or `vendor/`. The digest is the Linux x86-64 build's;
rustc's precompiled wasm32 standard library differs by host.

Sites without crates.io:

- **A registry mirror.** Set `IKI_CARGO_MIRROR` to its sparse index URL.
  Both steps use it instead of crates.io, and Cargo still checks each crate
  against `Cargo.lock`.
- **Vendored crates.** Run `cargo vendor --versioned-dirs` in the crate (with
  the patch above), commit `vendor/`, and add the crate's
  `.cargo/config.toml`. `--versioned-dirs` names each crate's directory as a
  registry does, so the module is byte for byte the one crates.io's give.

  ```toml
  [source.crates-io]
  replace-with = "vendored-sources"

  [source.vendored-sources]
  directory = "vendor"
  ```

  The build reads it from the crate's directory; nothing else is needed.

## Licence

Apache-2.0.
