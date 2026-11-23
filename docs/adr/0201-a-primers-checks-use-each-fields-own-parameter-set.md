# 0201. A primer's checks use each field's own published parameter set

- Status: Accepted
- Date: 2026-11-23

## Context

A scientist ordering primers asks four things of a design tool, and Benchling,
SnapGene, Primer3 and IDT's OligoAnalyzer all answer them: how warm does this
primer melt in the buffer my reaction runs in, does it fold back on itself, does
it pair with another copy of itself, and does it pair with its partner. A primer
whose 3′ end is tied up in a hairpin primes badly, and a stable primer dimer is
amplified in place of the target, so these are the numbers a design is rejected
for.

Iki already had a melting temperature. It had two, in fact: the DNA plugin
computed one in pure Python at 50 mM Na⁺ with no magnesium, and the plasmid
plugin's port (ADR 0200) computed the same quantity in Rust. Neither was the
temperature a PCR actually runs at, because a PCR buffer's magnesium and dNTPs
raise a primer's Tm by several degrees. Nothing computed a hairpin or a dimer at
all.

Two questions had to be settled before writing any of it.

**Which salt correction.** SantaLucia's 1998 unified model carries a salt
correction of its own (his equation 7), fitted to sodium. It is what Biopython's
`saltcorr=5` applies and what ADR 0200's port reproduces exactly, so it cannot
simply be replaced: the ported numbers are cross-checked against Biopython and
must stay so.

**Which parameter set for secondary structure.** Folding needs more than
nearest-neighbour stacks: loop penalties by size, special tri- and tetraloops,
dangling ends, initiation and symmetry terms. Several published sets exist and
they are not interchangeable, because each set's loop and initiation terms are
fitted against its own stacks.

## Decision

**Each field gets the published parameter set that field was measured under, and
each one is cited in the code and in the tests.** Nothing is mixed.

### Melting temperature: SantaLucia with Owczarzy's salt corrections

`thermodynamics` keeps SantaLucia's stacks, mismatches and initiation, and gains
a choice of salt correction:

- **SantaLucia's own** (1998, equation 7) for the fixed-condition temperature
  the plasmid plugin's port quotes, so ADR 0200's parity with Biopython is
  untouched;
- **Owczarzy's measured corrections** for a buffer: the monovalent equation
  (_Biochemistry_ 43:3537, 2004, equation 22), which that paper found independent
  of a duplex's length, and the divalent one (_Biochemistry_ 47:5336, 2008,
  equation 16) with its length term, where enough magnesium is free of the
  dNTPs to matter. Between the two regimes — R = √[Mg²⁺]/[Mon⁺] from 0.22 to
  6.0 — they compete, and three of the divalent coefficients become functions
  of the monovalent concentration, as that paper sets out.

Every coefficient and both regime boundaries are cited where they are written.
A primer's own `melting_temperature` field is now the buffer temperature: 50 mM
Na⁺, 1.5 mM Mg²⁺, 0.6 mM dNTPs, 25 nM of each strand, which is Primer3's default
PCR buffer. It reads several degrees above the sodium-only figure it replaced,
and that is the point of it.

An oligo that is its own complement — `GCGCGC`, and every palindrome — is
handled as SantaLucia's self-complementary case: there is no second species in
the tube, so all the strands are that one and the duplex carries his symmetry
term. This is what the DNA plugin's pure-Python action did and what Biopython's
`Tm_NN` does with `selfcomp=True`; ADR 0200's port, following `priming.py`,
never applied it, and this restores the agreement.

### Secondary structure: the NNDB DNA set, used whole

`structures` is a dynamic program over the pairs an oligo can make, with bulges
and internal loops inside the helix, reporting the most stable hairpin,
self-dimer and hetero-dimer by free energy at 37 °C, with the pairs and a
drawing. Its parameters are the **DNA set of the Nearest Neighbor Database**
(Turner and Mathews, _Nucleic Acids Res_ 38:D280, 2010; Mittal, Turner and
Mathews, _J Mol Biol_ 436:168549, 2024), the set the Mathews lab maintains for
RNAstructure, taken whole: stacks, terminal mismatches, dangling ends, the loop
table for 1 to 30, the special tri- and tetraloops, initiation, symmetry and
Ninio asymmetry.

It is used whole rather than mixed, for a specific reason. The NNDB DNA stacks
rest on the SantaLucia lab's own measurements and its ΔG°37 values equal
SantaLucia 1998's to 0.1 kcal/mol. But its initiation conventions differ: NNDB
charges a duplex 1.0 kcal/mol to open and nothing for an A·T end, where
SantaLucia and Hicks charge about 1.0 at each end and 0.05 per terminal A·T.
Taking loop penalties from one set and initiation from the other would invent
about a kcal/mol of chemistry that nobody measured.

NNDB was also the only candidate every table of which could be read in full.
SantaLucia and Hicks' loop and special-loop values are in a paywalled
supplement. Primer3's copies are GPL-licensed data files whose tetraloop
entropies are in the wrong scale, and its own maintainers state that its
secondary-structure numbers are not meant to be accurate predictions and are
shown only to warn a user. RNAstructure's distribution is GPL-2.0, so the values
were transcribed from the NNDB's published web tables rather than from its
source tree.

**So Iki's structure free energies are deliberately not Primer3's**, and the
tests do not pretend otherwise. They check what should hold of any parameter
set: a hand-computed duplex to the hundredth of a kcal/mol, the orderings
Primer3's own suite asserts, that nothing is reported where nothing pairs, and
the published measurements the melting temperatures can be checked against.

### primer3-py is not a dependency

`primer3-py` is GPL-2.0-or-later. It would be acceptable as a test-only
dependency of an Apache-2.0 project, but it would also put a GPL package in a
lockfile that a deployment's image is built from, and its parameter files are
not the sources this decision rests on. Primer3's own committed
`test/oligotm.txt` reference rows are used instead, written into the tests as
values with their provenance: the eight Owczarzy rows come back to within a
twentieth of a degree.

### The modules are built, never committed

The checks compile to a WebAssembly module the DNA plugin's action archive
carries, `plugins/dna/data/oligo-checks.wasm`, under the pattern ADR 0200
established: no `.wasm` is tracked in git, and git holds the Rust and the
module's SHA-256 digest. The plasmid plugin's module gains the checks too, for
the primer pair, so its digest moves with them. At this decision they are
`4708fd931ff281be3e9a88ecbe5b118b2ca89fef1796637c5c121d2b39dfe1a7` and
`31c83c4274ff4e1ffc2f8203cb0fd7318c270a460293f94df04c0a6f2f0f0b4d`, and the
tracked files are the authority.

Both digests are of the Linux x86-64 build, for the reason ADR 0200 measured:
the build is reproducible on one platform but not across platforms, because
rustc links its own precompiled `wasm32-unknown-unknown` standard library.
`scripts/build-oligo-checks-wasm.sh` therefore behaves as its sibling does — it
builds the module for the tests on this machine, compares it with the digest
only where that means something, and writes the digest from a Docker build of
the server image's wasm stage. The deployment CLI verifies it again before the
bytes reach a worker archive or the preinstalled worker image.

Both digests above have since been produced by two independent builders from
this commit's Rust: Docker `linux/amd64` running that stage, and the Linux
x86-64 test VM compiling natively in the merge gate's `--check`. They agree to
the byte. That is a measurement of these two modules, not an inference from
ADR 0200's: it says the build is reproducible within Linux x86-64, and says
nothing about any other host, where it is not expected to be.

The same function is exported from the server's own `iki-wasm` as
`iki_check_oligos`, so a primer view in the browser and a run on a worker quote
the same free energy.

## Consequences

A primer's record now carries what a scientist would otherwise paste into
OligoAnalyzer: its Tm in the reaction's buffer, the free energy of its hairpin
and self-dimer, how firmly its 3′ end is held, and its longest run of one base.
They are indexed fields, so a plate of primers can be sorted and filtered by
them rather than read one at a time.

The melting temperatures on existing primer records change, upward by several
degrees, when the action is run again. Nothing is deployed yet, so no record is
being rewritten in the field; after M1 a change of this kind would need its own
migration of derived values.

Iki's structure free energies will not match Primer3's, IDT's or Benchling's to
the decimal, because those tools use different parameter sets and in Primer3's
case a knowingly rough one. Where a scientist compares numbers across tools they
should expect agreement in the ordering and in what counts as concerning, not in
the digits. The code says which set it uses and the field descriptions say what
the number means, which is the most a tool can honestly offer.

The dynamic program is quadratic in an oligo's length. The module accepts at most
500 oligos of at most 200 bases, which bounds a batch, and each call runs with a
fuel budget so no request can hold a run up.
