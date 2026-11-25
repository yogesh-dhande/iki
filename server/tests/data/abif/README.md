# The AB1 trace the file metadata tests read

`3730.ab1` is a capillary sequencing trace written by an Applied Biosystems 3730xl's data
collection software, from Biopython's test suite (`Tests/Abi/3730.ab1` in the Biopython
1.88 source distribution). `server/tests/test_file_metadata.py` checks what Iki reads of
it against the tags themselves (ADR 0224): the instrument (`HCFG` 3, `3730xl`), its name
(`MCHN` 1, `ABI-3730-XL-1404-021`), its serial number (`HCFG` 4, `SerialNumber=1404-021`),
the data collection software's version (`SVER` 1, `3.0`) and the run protocol (`RPrN` 1,
`LongSeq50_POP7_2`); and its run start, 2009-12-12 09:56:53.

It's copied unmodified from
<https://files.pythonhosted.org/packages/f6/a0/cf657d076ec56a5f9e5c29a560c1f97b8eb6ae6608dc8bc95b4e2437f129/biopython-1.88.tar.gz>
(SHA-256 `9aaa31c0bda4d059f7b2ee00bfdb5cbb73ade3057aa4b737a7cc0187091d071a`, the hash
`uv.lock` pins Biopython 1.88 by). The file's own SHA-256 is
`e4663e4db40232576ccdda5b878dddb01ef80a3d1b032941ba053146ce53f77b`. It has no header of
its own, so it's under the Biopython License Agreement, as Biopython's files are unless
they say otherwise; its text is in `BIOPYTHON-LICENSE.rst`, and the repository's `NOTICE`
names it.
