# FASTA and HELM readers

Import `loadParsers` from `@ikisci/science/parsers`. Calling it fetches the
packaged WebAssembly binary and returns synchronous readers and a melting-temperature calculation.
A supplied `URL` selects a self-hosted asset. A supplied `WebAssembly.Module`
uses previously compiled bytes. Reuse the readers across calls. For large
files a Web Worker keeps parsing off the main thread and can release the
instance's memory when terminated.

```js
import { describeHelm, loadParsers } from "@ikisci/science/parsers";

const { readFasta, readHelm } = await loadParsers();
// Synthetic examples.
const file = readFasta(">row-a\nAC-T\n>row-b\nA-GT\n", "nucleotide");
if (file.kind === "alignment") console.log(file.consensus, file.agreement);
const [peptide] = readHelm(["PEPTIDE1{G.A}$$$$V2.0"]);
console.log(describeHelm(peptide));
```

`readFasta(input, alphabet?)` accepts a UTF-8 `Uint8Array` or a string. Its
optional alphabet is `"nucleotide"`, `"protein"`, or `null` to infer it.
`kind: "alignment"` returns the aligned rows, alphabet, column count,
conservation, consensus and agreement percentages. Each row contains its
identifier, description, aligned text, ungapped length and one-based starting
position. `reverse` marks a descending coordinate span. `kind: "sequences"`
returns the inferred or declared alphabet and a list of identifiers,
descriptions and ungapped lengths. `kind: "error"` includes a message and a
one-based line number when available.

`readHelm(texts)` accepts an array of HELM strings and returns readings in
the same order. Valid readings contain the canonical string, polymers,
connections, groups and a flag for HELM 1 input. For an invalid string
`isProblem(reading)` narrows the result to its message (`error`) and zero-based
Unicode character offset (`at`). `describeHelm(reading)` gives an English
description for either result. Each notation may occupy up to 1,000,000 UTF-8
bytes. The reader throws a `RangeError` before copying requests over 64 MiB
into WebAssembly memory.

`meltingTemperature(bases)` accepts a supplied unambiguous DNA oligonucleotide,
case-insensitively. It reports degrees Celsius under the same fixed conditions as
the sequence entry, rounded to 0.1 °C. Unsupported or too-short input returns `null`.

Workers using the module's existing JSON protocol can call
`requestJson(operation, request)` with `"helm"`, `"render"` for CEL templates,
or `"rules"` to evaluate a rules request. It returns the serialized JSON reply.
