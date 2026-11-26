# The FCS files the file metadata tests read

Two flow cytometry files, each written by its cytometer's software, from fcsparser's test
data (<https://github.com/eyurtsev/fcsparser>, commit
`da70aaa7ec92ff3bd9ce00aec4eea7c77ee8c096`), copied unmodified. fcsparser is MIT
licensed; its licence is in `FCSPARSER-LICENSE.txt`, and the repository's `NOTICE` names
them. `server/tests/test_file_metadata.py` reads what each says of its cytometer and
operator (ADR 0224).

| File                      | From                                                                  | SHA-256                                                            | What the tests read in it                                                                                                                                                                |
| ------------------------- | --------------------------------------------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cytek-aurora-header.fcs` | `fcsparser/tests/data/FlowCytometers/cytek-nl-2000/sample_header.fcs` | `1961e20bab436832ab1fad6f3563993d27181b263d8cc5d54274173b628e1fc3` | FCS 3.1's HEADER and TEXT segments only, as SpectroFlo wrote them: `$CYT` Aurora, `$CYTSN` N0354, `$OP` Admin, acquired 2020-07-22 18:39:40.59                                           |
| `cyflow-cube-8.fcs`       | `fcsparser/tests/data/FlowCytometers/cyflow_cube_8/cyflow_cube_8.fcs` | `08b8ef7fc49d34c56543551cc4c16bbf50c85af49d89b3abbe40d6f6a5d6a8d9` | An FCS 3.0 file from a Sysmex CyFlow Cube 8: `$CYT` Cube_15, `$CYTSN` 0131011431, `$OP` USER; its `$BTIM`, 09:42:05:509, is no time FCS 3.0 defines, so when it was acquired isn't known |
