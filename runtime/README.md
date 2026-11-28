# iki-plugin-runtime

The readers Iki's plugins' actions share, so that two analyses of the same file read it
alike:

- `iki_runtime.text`: a file's text or bytes, decompressed by its first bytes (gzip and
  BGZF, bzip2, xz), whatever its name says.
- `iki_runtime.fastq`: FASTQ reads, wrapped or not, as Cock et al. (2010) define them.
- `iki_runtime.airr`: AIRR rearrangement files, and a row's locus.
- `iki_runtime.helm`: HELM, read by iki-wasm as Iki reads it, into its canonical
  reading: polymers, monomers at canonical positions, connections and groups.

It uses Python's standard library only, for Python 3.8 or later. Only its HELM reader
needs more: wasmtime (`iki-plugin-runtime[helm]`), which it imports when it reads HELM,
and iki-wasm's module of the Iki it runs with.

## In an Iki environment

Declare it, and Iki installs the copy its release ships:

```yaml
python: { version: "3.12", requirements: requirements.txt, iki_runtime: true }
```

The environment's build digest includes the wheel's, so an Iki upgrade that changes the
package rebuilds the environment. `iki plugin validate` refuses an action whose script
imports `iki_runtime` in an environment that doesn't declare it.

To read HELM, declare its reader and pin wasmtime in the requirements:

```yaml
python: { version: "3.12", requirements: requirements.txt, iki_runtime: { helm: true } }
```

The environment gets iki-wasm's module at `share/iki-runtime/iki_wasm.wasm` under its
prefix, compiled there for its machine, and its SHA-256 joins the build digest.

## In your own image

Install the version of your Iki deployment:

```sh
pip install iki-plugin-runtime==0.4.0
```

```python
import iki_action as iki
from iki_runtime import fastq, text


def summarize(run):
    reads = sum(1 for _ in fastq.read(run.file(run.input("reads")["values"]["read_1"])))
    out = run.output()
    out.metric("reads", reads)
    return out


iki.main(summarize, refuse=(ValueError, *text.UNREADABLE))
```

An image that reads HELM puts iki-wasm's module, `iki_wasm.wasm` from its Iki's
iki-spec, at `share/iki-runtime/iki_wasm.wasm` under its Python's prefix, or names it in
`$IKI_RUNTIME_WASM`, and can compile it there with
`python -m iki_runtime.helm precompile <module>`.

Each release publishes the same file it ships, built from Iki's repository
(`runtime/`) by a pinned build backend, so its SHA-256 is the same wherever you get it.
