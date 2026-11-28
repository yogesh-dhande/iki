# Liquid handling: transfers as a Tecan worklist

Use this plugin when your lab records cherry-picks, normalisations or plate
reformatting in Iki, and runs them on a Tecan liquid handler. Record each **Plate**
as the worktable holds it, and each **Transfer** from a well of one plate to a well
of another. Then export the transfers as a **Tecan worklist** (`.gwl`), the file
Freedom EVOware and FluentControl run with their _Worklist_ command. You can export
one transfer from its page, or pick many, or a whole table, and export them as one
worklist.

No format Iki ships covers this, and labs otherwise write these files by hand or with
a spreadsheet macro. Here, the worklist is written from the transfers as recorded, at
the revisions you chose, and each download is in the audit log with the file's SHA-256.

## What the worklist holds

Each transfer is three lines, in the order you exported them, each ending CRLF:

```text
A;Compound source 1;CP-0412;384 Well Greiner;34;;2.5;DMSO Free Single;;;
D;Assay plate 7;AP-0007;96 Well Corning Flat;9;;2.5;DMSO Free Single;;;
W;
```

- `A` aspirates from the source plate, and `D` dispenses into the destination. Each
  names the plate by its label, barcode and labware type.
- The well is a position, counted down the columns: on a 96-well plate A1 is 1,
  B1 is 2 and A2 is 9. On a 384-well plate, B3 is 34.
- The volume is in µL, to 0.01 µL. A transfer recorded in another unit is converted.
- The liquid class is the transfer's own, if it has one. Otherwise the method's is
  used.
- `W` washes or replaces the tips after every transfer, so no two transfers share a
  tip.

The writer refuses a transfer the file can't express correctly, and the export fails,
naming that transfer. It refuses:

- a well that isn't on its plate;
- a volume that is zero, or rounds to zero;
- a volume in a unit that isn't a volume;
- a plate the person exporting can't read.

Two changes don't need a refusal, and are noted instead. A volume with more decimal
places than 0.01 µL is rounded. A `;` or line break in a label is replaced with `_`.
The notes appear in the export dialog and in the audit log.

The file is UTF-8. EVOware reads worklists in the Windows code page, so keep plate
labels and liquid classes to ASCII.

## How it runs

The writer is Rust, in `writers/tecan-worklist`, built against Iki's `iki-writer`
SDK (ADR 0230), the version its `Cargo.lock` pins with its checksum. Iki runs it in its export sandbox, which has no network, no secrets
and no files. Each export gets a fresh process holding only the records the person
exporting can read. The plugin tracks the module's SHA-256 in
`writers/tecan-worklist.wasm.sha256`. Your deployment's image build rebuilds the
module from this source and refuses to install it unless the digest matches.

## Adapt it for your lab

1. Copy this directory into your own repository. Set the namespace and plugin name in
   `plugin.yaml`, and pick record prefixes unique to your deployment in `types/`.
2. Match the labware to your worktable. Add plate formats your lab uses to `wells` in
   `types/plate.yaml`, and their row and column counts to the writer's `Plate` in
   `src/lib.rs`.
3. Change the writer if your method needs something different, such as a wash only
   between sources (`W;` after each change of source plate) or a flush (`F;`). Run its
   tests natively:

   ```sh
   sh "$IKI_CHECKOUT/worker/crates/iki-writer/build-writer.sh" fetch writers/tecan-worklist
   sh "$IKI_CHECKOUT/worker/crates/iki-writer/build-writer.sh" test writers/tecan-worklist
   ```

4. Build the module the way your deployment's image will, and write its digest. This
   needs Docker:

   ```sh
   iki plugin writers . --update
   iki plugin validate .
   python -m pytest -q tests/
   ```

   Commit the digest file with the change to the Rust, so a reviewer sees both. Never
   commit the `.wasm`. The file also says what the module was built with (the source,
   the `iki-writer` the lock pins, the writer builder and the build script), so if a
   deployment's build makes another module, it says which of them differs.

5. After renaming a fresh copy, delete the example's `migrations/` and generate your
   own with `iki plugin makemigrations .`. Then pin the plugin in your deployment's
   `plugins.lock.yaml` and turn on the chart's export sandbox (`sandbox.enabled`).

A site without access to crates.io can vendor the writer's crates
(`build-writer.sh vendor writers/tecan-worklist`) into the crate, or build with
`IKI_CARGO_MIRROR` pointing at a registry mirror. Both give the same module, so the
digest doesn't change.
