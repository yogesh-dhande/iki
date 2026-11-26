# Test data

Three QuantStudio 7 Flex results exports, kept byte for byte as published in
Benchling's [allotropy](https://github.com/Benchling-Open-Source/allotropy) at
commit `5bcc4c92b5eba5b2b1febe6efbc98bab6a3b5685`, under
`tests/parsers/appbio_quantstudio/testdata/`. allotropy is under the MIT
licence, which is in [`allotropy-LICENSE.txt`](allotropy-LICENSE.txt).

| File                                          | allotropy's name                      | What it is                                                                                                                                         | SHA-256         |
| --------------------------------------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| `qs7-flex-3utr-standard-curve.txt`            | `example_quantsudio_results_only.txt` | A standard curve run from 1 October 2024: a 3′UTR target (VIC): two standards in triplicate and 18 unknowns, the results-only export               | `4f6fc56e…5859` |
| `qs7-flex-rnasep-relative-standard-curve.txt` | `appbio_quantstudio_example07.txt`    | Thermo Fisher's RNase P relative standard curve example (2010): 13 standards, three no-template controls and four expired calibrations             | `75813e7f…0bbe` |
| `qs7-flex-tgfb-comparative-ct.txt`            | `appbio_quantstudio_example06.txt`    | Thermo Fisher's comparative Cт example (2010): TGF-B against GAPDH in lung and liver, liver the reference sample; exported on Windows (CRLF lines) | `8de59f27…7e0b` |

The tests' expected values are what each file states, checked against its own
standard curves (see `../test_quantstudio.py`). `.gitattributes` keeps the
files' line ends as they are, since a record's SHA-256 is of the exact bytes.
