# 0200. One implementation places primers and cuts DNA, and the enzyme table stays where its licence is

- Status: Accepted
- Date: 2026-11-22
- Amended: 2026-11-23 — the browser's copy is a separate entry, when a page needs
  it, not exports of the shared module

## Context

Finding where a primer binds a template, measuring the duplex it forms, and
working out what restriction enzymes cut a molecule into are three of the things
every scientist expects of a lab platform, and three of the things Iki shows in
several places at once: a run computes them on the backend, a map draws them in
the browser, and a web package will offer them to applications outside Iki.

They were Python in one plugin, `plugins/plasmid/scripts/priming.py` and
`plugins/plasmid/scripts/cloning.py`, leaning on Biopython for the melting
temperature and for the restriction enzymes. Anything that wanted the same
answer elsewhere would have had to compute it again, and two implementations of
a nearest-neighbour model do not stay equal.

The web-package work establishes the shape of the answer for the
fixed-condition melting temperature: the science lives in the Rust crate behind
`iki-wasm`, the plugin's action archive carries a module compiled from it, and
each runtime calls that module rather than keeping its own copy. This decision
applies that shape to primer placement and restriction digests, and settles a
licence question the digest raises.

Restriction enzyme recognition sites and cut offsets come from REBASE (Roberts,
Vincze, Posfai and Macelis, _Nucleic Acids Res_ 51:D629-D630, 2023,
<https://doi.org/10.1093/nar/gkac975>). Iki reaches them through Biopython's
generated `Bio/Restriction/Restriction_Dictionary.py`. Compiling that table into
a data file in this repository would make Iki a redistributor of REBASE's data
for the first time, and REBASE's own terms do not clearly permit that:

- REBASE's site says it is "an open database provided to benefit the scientific
  community" and that "adaptation or distribution of information from REBASE
  should also remain open and accessible to the public" -- an intent, not a
  grant, naming no permitted acts;
- REBASE's data files carry "Copyright (c) Dr. Richard J. Roberts. All rights
  reserved";
- the REBASE pages incorporate New England Biolabs' Terms of Use, which forbid
  copying other than "non-commercial individual reference purposes" and forbid
  compiling, distributing, republishing or creating derivative works without
  written agreement;
- Biopython's licence covers Biopython's file and says nothing about the REBASE
  content in it, so relying on it inherits a silence rather than a permission.

## Decision

**The science moves to Rust.** `worker/crates/iki-wasm` gains three modules:

- `thermodynamics`: the melting temperature of a short duplex, mismatches and
  all, by the unified nearest-neighbour model (SantaLucia, _PNAS_ 95:1460-1465,
  1998, <https://doi.org/10.1073/pnas.95.4.1460>), with Allawi and SantaLucia's
  Watson-Crick stacks and internal mismatches, SantaLucia and Peyret's terminal
  mismatches, Table 2’s initiation and symmetry terms, equation 3 for the
  temperature, and equation 8 for the monovalent salt correction;
- `priming`: where primers bind, on both strands, round a circle's origin and
  never past a linear end, placed by their 3' ends with a position-weighted
  mismatch penalty, and what a pair of facing sites amplifies;
- `digest`: where enzymes cut, with IUPAC ambiguity in the recognition site,
  both strands for a non-palindromic enzyme, each cut's overhang kind, length
  and bases, and the fragments and gel bands that follow.

A standalone crate compiles them for `wasm32-unknown-unknown` with no host
imports, from the same source files `iki-wasm` holds, so every runtime that
calls them answers identically.

As first accepted, this decision also exported them from the server's own
`iki-wasm`, for the browser. That module is the one every page loads for its
file readers, and `@ikisci/science` holds its gzip size to a budget; with the
primer, digest and oligo-check exports it went from 594 kB to 650 kB, over that
budget, for code no page calls yet. So they are not exported there. When a page
needs them, the browser gets them as an `@ikisci/science` entry of its own,
compiled from the same source, loaded only by the pages that use it, and with a
size budget of its own.

**No `.wasm` is tracked in git.** What git holds is the Rust and the module's
SHA-256 digest, `plugins/plasmid/data/plasmid-tools.wasm.sha256` — at this
decision `ffcbfe9fed8e35d9bc2f907c1f3f5363e4b0529e2a9bebbe62e3eba5baf6bd33`,
and the tracked file is the authority, since every deliberate change to the Rust
moves it.

**The digest is of the module built for Linux x86-64**, which is what every
image, CI job and deployment runs. This is not an assumption about what should
follow from remapped paths; it is what three builders were measured to produce
from this commit's Rust:

| builder                                                                          | digest                    |
| -------------------------------------------------------------------------------- | ------------------------- |
| Docker `linux/amd64`, the server image's wasm stage, run from a macOS arm64 host | `ffcbfe9f…bd33`           |
| the Linux x86-64 test VM, natively, via `--check` in `make check-rust`           | `ffcbfe9f…bd33` (matched) |
| macOS arm64, natively                                                            | `2a76cea5…c8fd` (differs) |

So the build is reproducible **within** Linux x86-64 — two different builders,
one in a container and one on bare metal, agree to the byte, with the checkout's
path and the cargo home remapped out of it — and is **not** reproducible across
host platforms, nor expected to be: rustc links its own precompiled
`wasm32-unknown-unknown` standard library, and the copy shipped for macOS arm64
is not byte-identical to the one shipped for Linux x86-64. A macOS arm64 build
matching this digest would be a surprise, not a requirement.

This was worth measuring rather than reasoning about. A byte-identical claim
across platforms was asserted elsewhere in the project on the same reasoning and
was wrong for a larger module; one digest claimed for both platforms would have
failed the check for every developer on a Mac. Each step therefore does what it
can honestly do:

- `scripts/build-plasmid-tools-wasm.sh` builds the module for the tests on this
  machine, and compares it with the digest only where the comparison means
  something, on Linux x86-64; elsewhere it prints what it built and says why it
  isn't comparing;
- `--update` builds it through the server image's wasm stage, in Docker, so the
  digest it writes is the Linux x86-64 one whoever runs it;
- `--build-only` is what that stage itself runs: it compiles and compares
  nothing, since it is where the digest's bytes come from;
- iki-spec's distribution build backend runs the plain build, so a wheel carries
  the module for the platform it was built on;
- the merge gate's Rust job, on Linux x86-64, runs `--check`, which is where a
  module that has drifted from its source is caught — and is the only place the
  tracked digest is actually compared, since a developer on a Mac cannot.

`iki deploy build` exports each pinned plugin with `git archive`, which carries
the digest and not the module. So iki-spec's wheel carries the built modules
(`iki_spec.modules`), and the deployment CLI takes the module from there, checks
it against the exported digest, and only then writes it into the plugin
directory it bundles — so the bytes that reach a worker archive and the
preinstalled worker image are bytes built from the Rust at that commit, for the
platform the worker runs on. A worker needs no Rust toolchain. A module that is
missing from both places, or that does not match, fails the build rather than
the first run, and the refusal says which of the two it is.

The CLI compares the bytes where the comparison can hold, on Linux x86-64, and
elsewhere requires the module to be there without comparing it. That is the
threat model rather than a concession: the only bytes that ever reach a worker
are the Linux x86-64 ones, since that is what a worker image is, so those are
always checked; a developer building a deployment on a Mac is building for a dev
cluster, and a comparison there could only fail. The dev cluster gets canonical
bytes anyway, because `deploy/dev/build-wasm.sh` takes both modules out of the
Docker stage.

## Consequences

One answer everywhere, and a place to put the next piece of sequence science.
The plugin's refusals read better in one respect: naming an enzyme whose cut
REBASE does not state used to raise a `TypeError` from inside a digest, and now
refuses with a message that names the enzyme.

A first-party plugin can now carry a compiled module without a binary entering
git, and the mechanism is general: a plugin tracks `<module>.sha256`, a build
produces the bytes, and the deployment CLI vouches for them. The cost is that
building a release needs an iki-spec built from the same commit as the plugins
it pins, which is what `iki deploy build` already assumes for everything else.

The plugin gains Wasmtime as a dependency and keeps Biopython, which it needs
for the enzyme descriptions and for the library alignment. A run pays one module
instantiation per call, with a fuel budget and a memory cap, so no template or
set of primers can hold a run up.

Melting temperatures are reported to one decimal place, rounded as a decimal
with halves going to the even digit, which is what the existing records hold.
A duplex with no published parameter for one of its pairs of neighbours has no
temperature rather than a guessed one.

The browser cannot yet cut a molecule without a table from somewhere, and a
deployment that wants to is on its own licence for it. That is the cost of not
redistributing REBASE.
