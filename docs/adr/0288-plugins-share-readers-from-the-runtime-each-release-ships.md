# 0288. Plugins share readers from the runtime each release ships

- Status: Accepted
- Date: 2026-11-27
- Extends: [0059](0059-workers-fetch-plugin-code-and-build-environments.md),
  [0287](0287-scripts-read-and-write-runs-with-one-vendored-helper.md)

## Context

Plugins' scripts read the same kinds of file, each with code of its own:

- Five of them read a file that might be gzipped, each sniffing its first bytes in
  its own way. None read bzip2 or xz.
- Two read FASTQ: the sequencing plugin's summary and the plasmid sequencing
  plugin's read count. They disagreed about a wrapped record until one was fixed.
- Two read AIRR rearrangement files. One inferred a row's locus only for
  immunoglobulins, and the other for any three letters.

Each copy is a place where two of Iki's analyses can read the same file differently.
Iki's action helper (ADR 0287) can't hold these readers. It is one file that every
plugin vendors and that a person can read in a sitting, and the readers would make it
neither.

Readers are also unlike the action helper in how they can travel. The action helper
has to run in any image, including one Iki doesn't build, so it travels inside the
plugin. A reader is needed in environments Iki does build: the Python and conda
environments of the backend and self-hosted tiers (ADR 0059). Iki can install a
package into those. A lab's own image can install a package from PyPI.

How other systems share code with the jobs they run:

- Bazel and Nix ship one toolchain with each release of the build and key every
  build by its digest.
- Python's own `ensurepip` installs the pip that ships with that Python.
- GitHub's runner images carry their tools at known versions.

A dependency pinned in each plugin's requirements file would work too. But every
plugin's file would then name its own version, upgraded on its own schedule, and the
duplication would come back as version skew.

## Decision

**One package, `iki-plugin-runtime`, imported as `iki_runtime`.** Its source is in
Iki's repository under `runtime/`. It uses only Python's standard library, for
Python 3.8 or later, as the action helper does. It has:

- `iki_runtime.text`: `open_text`, `open_bytes` and `compression`. These read a
  file's compression from its first bytes, never from its name. They handle gzip,
  which BGZF also is, bzip2 and xz. `UNREADABLE` names what a damaged compressed
  file raises.
- `iki_runtime.fastq`: `read` and `reads`, which follow Cock et al. (2010). Bases and
  qualities may each be wrapped over several lines. A record ends when it has as many
  qualities as bases. Errors say which read is wrong.
- `iki_runtime.airr`: `read` and `rearrangements`, which check the header and each
  row's width, and `locus`. `locus` returns the row's own locus, or the locus its V
  and J calls share, from the eight loci the schema names.

**Each release ships it, at the release's version.** The version in
`runtime/pyproject.toml` is Iki's, and the release refuses a tag that doesn't match
it, as it does for the chart and the worker. The package pins its build backend
exactly (`uv_build==…`). So the same source always builds the same file, and the
release builds it twice to check. iki-spec carries that wheel (`iki_spec.plugin_runtime`)
the way it carries the modules plugins' actions call:

- `scripts/build-plugin-runtime.sh` builds it in a checkout, and `make test-wasm`
  runs that script;
- the server image has a stage that builds it;
- iki-spec's build backend builds no wheel without it.

The release publishes the same file to PyPI by trusted publishing, from the `pypi`
environment with no stored token, and attaches it to the GitHub release.

**An environment opts in.** `python: { …, iki_runtime: true }`, or
`conda: { …, iki_runtime: true }` for a conda environment's own Python, installs the
wheel of the Iki that builds the deployment. The wheel isn't listed in the
requirements file.

- `iki deploy build --workers` puts the wheel beside the build script. It installs
  the wheel with `uv pip install --require-hashes --no-deps --no-index` after the
  environment's own packages.
- Each plugin bundle that needs the wheel carries it at `.iki-runtime/` for
  self-hosted workers, and `GET /worker/v1/plugins` names it as the environment's
  `runtime`.

**Its digest is part of the build.** The environment's build definition gains
`iki_runtime_sha256`, so its digest is keyed by the wheel's bytes. Different bytes
mean a different build. An upgrade that changes the runtime rebuilds the environments
that use it, and leaves the others' digests and cached layers alone. A self-hosted
worker computes the digest from the wheel it was given, as it does for the
requirements file. It refuses a build whose digest isn't the one the server expects
before it installs anything.

**A script that needs it runs where it is.** `iki plugin validate` follows each
action's `./…py` command, and the modules beside it that the script imports. It
refuses one that imports `iki_runtime` in a Python or conda environment that doesn't
declare it (`runtime-not-installed`). An image's packages are its own, so for an
image the validator says nothing.

The sequencing, sequencing analysis, antibody rearrangements, proteomics and plasmid
sequencing plugins now read with it, and each copy they had is gone.

### HELM

`iki_runtime.helm` reads HELM with iki-wasm, the Rust reader Iki's forms, identities
and server read it with, so a plugin's chemistry sees the canonical reading Iki stores:
polymers in canonical order and numbered again, fixed repeats written out, rings at
their least rotation, each monomer at its canonical position. The oligo plugin reads
with it, and its own HELM reader is gone.

- **The wheel stays standard-library only and byte-reproducible.** The module isn't in
  it: it's iki-wasm's own build, the one iki-spec carries and `server/iki_wasm.wasm.sha256`
  pins. `iki_runtime.helm` imports wasmtime only when it reads HELM, and declares it as
  the package's `helm` extra; the rest of the runtime imports nothing else.
- **An environment opts in with `iki_runtime: { helm: true }`** (`iki_runtime: true` is
  the runtime without it). Its requirements pin wasmtime, as the dna plugin's do
  (`helm-needs-wasmtime` otherwise), and a script that imports `iki_runtime.helm` in an
  environment that doesn't opt in is refused (`runtime-not-installed`).
- **The module travels beside the wheel.** A bundle carries it at
  `.iki-runtime/iki_wasm.wasm`, `GET /worker/v1/plugins` names it (`runtime_wasm`), and
  its SHA-256 joins the build digest as `iki_wasm_sha256`, computed alike by
  `iki deploy build` and the worker. The server image's iki-spec carries the module its
  wasm stage verified, with the pin beside it, and `iki deploy build` checks it against
  that pin again. A checkout's module is cargo's build on its own host, whose bytes
  differ from the pin's by the paths it was built in, even on Linux x86-64: it has no
  pin beside it and goes by its digest, which still names exactly the bytes a worker
  gets.
- **The environment has it at a fixed place**, `share/iki-runtime/iki_wasm.wasm` under
  its prefix, since a worker passes actions no environment of their own. The build
  compiles it there for its machine (`python -m iki_runtime.helm precompile`), so a run
  loads it in milliseconds rather than compiling it for seconds; compiled code that
  isn't a regular file only its owner can write is ignored and the module compiled
  again. Elsewhere `$IKI_RUNTIME_WASM` names it, and a Python with iki-spec (Iki's
  checkout, `iki plugin test`) uses iki-spec's.

## Consequences

- Two plugins can no longer count the same FASTQ file's reads differently, or give
  the same AIRR row different loci. A fix to a reader reaches every plugin in the next
  release.
- A plugin can't choose its own version of the runtime: it gets its deployment's. A
  plugin that needs something a later runtime added says which Iki versions it
  supports, as it does for any other feature.
- A runtime change rebuilds the environments that declare it. They are few, and each
  rebuild is the same uv install from the cache.
- Readers in any image work alike. A lab's own image installs the PyPI copy at its
  Iki's version, and so does the Python `iki plugin test` runs commands with, outside
  Iki's repository.
- The conda environment's lockfile must include Python, and its worker needs uv, as
  Python environments already do.
- An Iki upgrade that changes iki-wasm rebuilds the environments with its HELM reader,
  as one that changes the wheel rebuilds those with the runtime.
- A lab's own image that reads HELM with the runtime brings the module itself, from its
  Iki's iki-spec, at the fixed place or named by `$IKI_RUNTIME_WASM`.
- PyPI's pending publisher for `iki-plugin-runtime` has to exist before the first
  tagged release. It names this repository, `release.yml` and the `pypi` environment.

## Alternatives considered

- **A pinned requirement in each plugin.** Each environment's requirements file would
  name a version. The pins would drift apart, a lab's older plugin would hold an old
  reader, and the copies this removes would come back as versions.
- **Readers in the vendored action helper.** It would no longer be small enough to
  vendor and read. And most images that need a reader are ones Iki builds.
- **Vendoring each reader beside the scripts that use it, checked by digest.** That
  is what the helper does. It suits one small file with a stable contract. For
  several modules that grow, a lab would review and re-vendor each change, plugin by
  plugin.
- **Installing the newest release from PyPI at build time.** Builds would depend on
  the network and on PyPI, and a build's bytes on when it ran.
