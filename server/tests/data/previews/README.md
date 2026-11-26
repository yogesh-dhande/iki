# The files the file previews' readers are checked against

Files written by instruments and their software, from the test suites of open-source
readers of each format. The previews' readers (`worker/crates/iki-previews`, ADR 0226)
read them in `worker/crates/iki-previews/tests/`: each test checks the reading against what
the file says of itself, and where there is one, against what another reader's own tests
expect of it. Each file is copied byte for byte from the commit named; `.gitattributes`
and `.editorconfig` keep them that way. Two are renamed, as the table says.

## Sanger traces (ABIF)

From Biopython's test suite, at commit
[`86e9ea56`](https://github.com/biopython/biopython/tree/86e9ea56dbd67f51565fc9566e4f87a888f94454/Tests/Abi).
They have no headers of their own, so they're under the Biopython License Agreement, as
Biopython's files are unless they say otherwise. Its text is in
`../snapgene/BIOPYTHON-LICENSE.rst`. At this commit, the text is the same except that its
copyright line reads 1999-2026.

| File                          | What it is                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `3730.ab1`                    | `Tests/Abi/3730.ab1`: a 3730 trace of 1,165 calls, with quality values                                                                                                                                                                                                                                                                                                                                           |
| `310.ab1`                     | `Tests/Abi/310.ab1`: an old 310 trace of 868 calls (sample D11F), many of them N, and every quality value 0                                                                                                                                                                                                                                                                                                      |
| `biopython-abi-expected.json` | What Biopython's `Tests/test_SeqIO_AbiIO.py` (same commit) expects of the two files: each one's sequence, quality values, sample name and machine model. The values are copied from the test's own dictionaries (read with Python's `ast.literal_eval`, not run), so the readers are checked against Biopython's expectations without Biopython. This file is part of Biopython's tests, under the same licence. |

## Spectra (JCAMP-DX)

| File                      | What it is                                                                                                                                                               | From                                                                                                                                                                                                                                                                                                      | Licence                                                                                                                                                      |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `dupdec1.jdx`             | An infrared spectrum from a Perkin-Elmer 1605 FT-IR, 4,400 to 450 cm⁻¹, written compressed (DIF with DUP, with Y check values)                                           | Robert J. Lancashire's JCAMP-DX test data (University of the West Indies, Mona, first published at `http://wwwchem.uwimona.edu.jm:1104/spectra/testdata/`), from `data/Robert_J_Lancashire_test_data/` in [nzhagen/jcamp](https://github.com/nzhagen/jcamp/tree/9a4af7d3f0cf6aa8d27913426334dec1f5b06130) | The file says `##OWNER= public domain`. The nzhagen/jcamp repository is under the MIT licence, Copyright (c) 2013 Nathan Hagen (`NZHAGEN-JCAMP-LICENSE.txt`) |
| `pktab1.jdx`              | Cholesterol's mass spectrum as a peak table (`(XY..XY)`), 46 peaks up to its molecular ion at m/z 386                                                                    | The same                                                                                                                                                                                                                                                                                                  | The same                                                                                                                                                     |
| `jcamp-difdup.dx`         | A ¹H NMR spectrum of 16,384 points written by Bruker XWIN-NMR, compressed (DIF with DUP)                                                                                 | `test/data/compression/` in [andcastillo/jcampconverter](https://github.com/andcastillo/jcampconverter/tree/4ac325c04fcf24f1e02028bee58aeec94c227500), whose tests expect the two files to read the same                                                                                                  | MIT, Copyright (c) 2014 Luc Patiny (`JCAMPCONVERTER-LICENSE.txt`)                                                                                            |
| `jcamp-fix.dx`            | The same spectrum, uncompressed (fixed AFFN)                                                                                                                             | The same                                                                                                                                                                                                                                                                                                  | The same                                                                                                                                                     |
| `aspirin-1h.fid.dx`       | Renamed from `data/nmr/aspirin/1h.fid.dx`: aspirin's ¹H FID from a Bruker spectrometer (XWIN-NMR), real and imaginary parts as `##NTUPLES` pages of 8,192 points         | [cheminfo/jcamp-data-test](https://github.com/cheminfo/jcamp-data-test/tree/22da30867a3baec74a2c2c887382b87d0ba048dd); the repository doesn't say where the spectrum was measured                                                                                                                         | MIT, Copyright (c) 2020 cheminfo (`JCAMP-DATA-TEST-LICENSE.txt`)                                                                                             |
| `broad-carbon-bruker.jdx` | `data/nmr/bruker/broad-carbon-bruker.jdx`: a ¹³C spectrum of 1,024 points exported by TopSpin 3.7.0, whose `##FIRSTX` and `##LASTX` are the wrong way round for its data | The same                                                                                                                                                                                                                                                                                                  | The same                                                                                                                                                     |

## Mass spectrometry (mzML)

Both are from pyteomics' test suite, `tests/` at commit
[`bff2052f`](https://github.com/levitsky/pyteomics/tree/bff2052ff5dc986a722fe3b1aa8040a309f462d9/tests).
Pyteomics is under the Apache License 2.0, which is the same licence as Iki's own. Its text
is in the repository's `LICENSE`. Pyteomics has no `NOTICE` file.

| File                  | What it is                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tiny.pwiz.1.1.mzML`  | ProteoWizard's example indexed mzML 1.1 file: four spectra (an MS1, an MS2 with a precursor, an empty one and a MALDI spectrum) and two chromatograms (a TIC and an SIC). Its arrays are made-up numbers from ProteoWizard's example generator (`pwiz/data/msdata/Examples.cs` at [`511ab0a2`](https://github.com/ProteoWizard/pwiz/blob/511ab0a2affdcc3fa5aebe56d0195427dbe76ffc/pwiz/data/msdata/Examples.cs)), and the base peak and m/z range written beside them don't match them |
| `pyteomics-test.mzML` | Renamed from `test.mzML`: two profile spectra of 19,914 points each, written by ProteoWizard, and a TIC. The two spectra hold the same arrays, stored uncompressed (m/z 64-bit) in one and zlib-compressed (32-bit) in the other. Its index is stale: `<indexListOffset>` points past the file's end and the first spectrum's offset is one byte off. So it's what checks that a preview falls back to scanning the file                                                               |

## TIFF and workbook samples (`iki-previews`' TIFF and table readers)

Written by other programs than the readers, so the tests in
`worker/crates/iki-previews/tests/` don't only check the readers against a writer of their
own.

| File                                     | What it is                                                                                                                                       | Made by                                     |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------- |
| `ome_pyramid.ome.tif`                    | OME-TIFF, 2 channels (DAPI, GFP) × 2 focal planes, 128 × 96 16-bit, 64 × 64 tiles, LZW with the horizontal predictor, and two SubIFD levels each | tifffile 2026.11.16                         |
| `float_predictor3_bigtiff.tif`           | BigTIFF, big-endian 32-bit floats (`x × 0.5 − y × 0.25`, one NaN), 32 × 32 tiles, Deflate with the floating-point predictor                      | tifffile 2026.11.16, imagecodecs 2026.11.11 |
| `jpeg_ycbcr_tiles.tif`                   | RGB as YCbCr JPEG tiles, each a whole JPEG (no `JPEGTables`)                                                                                     | tifffile 2026.11.16, imagecodecs 2026.11.11 |
| `jpeg_rgb_strips.tif`                    | RGB JPEG strips with `JPEGTables`                                                                                                                | Pillow 13.0.0 (libtiff)                     |
| `palette_lzw.tif`                        | A 4-colour palette image, LZW                                                                                                                    | Pillow 13.0.0 (libtiff)                     |
| `bilevel_packbits.tif`                   | 1-bit bilevel, PackBits                                                                                                                          | Pillow 13.0.0 (libtiff)                     |
| `qpcr_plate_results.xlsx`                | A qPCR run's results from B3, with dates, an elapsed time, booleans and a hidden "Standard curve" sheet                                          | openpyxl 3.1.5                              |
| `calamine_any_sheets.xlsx`               | Visible, hidden, very hidden and chart sheets                                                                                                    | Excel, from calamine's tests                |
| `calamine_date_1904.xlsx`                | Dates and an elapsed time in the 1904 date system                                                                                                | Excel, from calamine's tests                |
| `calamine_rph.xlsx`                      | A shared string with phonetic runs (furigana)                                                                                                    | Excel, from calamine's tests                |
| `calamine_rich_text_shared_strings.xlsx` | Rich-text shared strings                                                                                                                         | Excel, from calamine's tests                |
| `calamine_password_protected.xlsx`       | A password-protected workbook (an OLE compound file)                                                                                             | Excel, from calamine's tests                |

The `calamine_*` files are `tests/any_sheets.xlsx`, `tests/date_1904.xlsx`, `tests/rph.xlsx`,
`tests/rich_text_shared_strings.xlsx` and `tests/password_protected.xlsx` of
[calamine](https://github.com/tafia/calamine) (master on 2026-11-25), unchanged but for
their names. calamine is MIT-licensed: Copyright (c) 2016-2025 Johann Tuffe; the licence is
its [`LICENSE-MIT.md`](https://github.com/tafia/calamine/blob/master/LICENSE-MIT.md).

The TIFFs, with `uv run --with tifffile --with imagecodecs --with numpy --with pillow`:

```python
import numpy as np, tifffile
from PIL import Image

c, z, y, x = np.mgrid[0:2, 0:2, 0:96, 0:128]
data = (c * 10000 + z * 1000 + (y * 2 + x) % 256).astype(np.uint16)
with tifffile.TiffWriter("ome_pyramid.ome.tif", ome=True) as tif:
    opts = dict(tile=(64, 64), compression="lzw", predictor=True)
    tif.write(
        data,
        subifds=2,
        metadata={
            "axes": "CZYX",
            "Channel": {"Name": ["DAPI", "GFP"]},
            "PhysicalSizeX": 0.325,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.325,
            "PhysicalSizeYUnit": "µm",
        },
        **opts,
    )
    tif.write(data[:, :, ::2, ::2], subfiletype=1, **opts)
    tif.write(data[:, :, ::4, ::4], subfiletype=1, **opts)

yy, xx = np.mgrid[0:40, 0:50]
f = (xx * 0.5 - yy * 0.25).astype(">f4")
f[3, 4] = np.nan
tifffile.imwrite(
    "float_predictor3_bigtiff.tif",
    f,
    bigtiff=True,
    byteorder=">",
    tile=(32, 32),
    compression="zlib",
    predictor=3,
)

y, x = np.mgrid[0:64, 0:96]
rgb = np.stack([x * 2, y * 3, x + y], -1).astype(np.uint8)
tifffile.imwrite("jpeg_ycbcr_tiles.tif", rgb, tile=(32, 32), compression="jpeg", photometric="rgb")
Image.fromarray(rgb).save("jpeg_rgb_strips.tif", compression="jpeg")

idx = ((x // 12 + y // 16) % 4).astype(np.uint8)
im = Image.fromarray(idx, "P")
im.putpalette([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255] + [0] * 252 * 3)
im.save("palette_lzw.tif", compression="tiff_lzw")
Image.fromarray((x + y) % 3 == 0).save("bilevel_packbits.tif", compression="packbits")
```

The workbook, with `uv run --with openpyxl==3.1.5`:

```python
import datetime as dt, openpyxl

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Results"
run = dt.datetime(2026, 3, 9, 14, 5, 30)
took = dt.timedelta(hours=1, minutes=42, seconds=7)
rows = [
    [
        "Well",
        "Sample",
        "Target",
        "Cq",
        "Quantity (copies/µL)",
        "Run started",
        "Run time",
        "Passed QC",
        "Notes",
    ],
    ["A1", "NTC", "GAPDH", "Undetermined", None, run, took, False, "no template control"],
    ["A2", "Donor 17 PBMC", "GAPDH", 18.4321, 152000.0, run, took, True, None],
    ["A3", "Donor 17 PBMC", "IL-6", 31.07, 0.000125, run, took, True, "  low copy, repeat  "],
    [
        "B1",
        "Standard 1e6",
        "GAPDH",
        15.002,
        1e6,
        dt.date(2026, 3, 9),
        dt.timedelta(days=1, hours=2),
        True,
        "Std curve slope −3.32",
    ],
]
for r, row in enumerate(rows, start=3):
    for c, value in enumerate(row, start=2):
        cell = ws.cell(row=r, column=c, value=value)
        if isinstance(value, dt.timedelta):
            cell.number_format = "[h]:mm:ss"
        elif isinstance(value, dt.datetime):
            cell.number_format = "yyyy-mm-dd hh:mm:ss"
        elif isinstance(value, dt.date):
            cell.number_format = "yyyy-mm-dd"
ws["E5"].number_format = "0.00"
curve = wb.create_sheet("Standard curve")
curve.sheet_state = "hidden"
curve.append(["log10 copies", "Cq"])
for row in [(6, 15.002), (5, 18.31), (4, 21.65)]:
    curve.append(row)
wb.save("qpcr_plate_results.xlsx")
```

The tests' expected rows for the workbooks are what the server's importer
(`server/src/iki_server/spreadsheets/workbooks.py`, through python-calamine 0.9.1) reads from them.

## SHA-256

```text
e4663e4db40232576ccdda5b878dddb01ef80a3d1b032941ba053146ce53f77b  3730.ab1
0a395d39520d24a89af4c77dd55fd7fb632e732a6da807ceb6ead0c2465cb887  310.ab1
5cd13e619c3cf917bb60c4a41a135eddf4689c7ee28496bc746ebe0345d3166f  biopython-abi-expected.json
16a4f8cb7aa60e7c0c8f477b16a66fcae8c342d2642a1115707f3df178b372dc  dupdec1.jdx
e9f80b5e36f5c4521e9d4d5e464a92dbbb4ec50e844b794272f338bc5dfd1485  pktab1.jdx
68e491faf98510abbc6e2ef5dc8b27f43245cbdc169e625c43f9c4300972ac74  jcamp-difdup.dx
26dd73451f788583b1bfad285ecd8feb10015b7889a6d23d9a09193ebd0e2f94  jcamp-fix.dx
9d15f7f4b81113d0619aa2eb559ed52f51615296ac716ac85bde816e564c6cfd  aspirin-1h.fid.dx
db0ff39a9f7be3dc12a9d76eaee0209b84f77321b44e291f0a2f88fa031bde4f  broad-carbon-bruker.jdx
711ac14b666f14817c208bd4d39b738e96ac827574c4639d8f8f6eebbfde9c83  tiny.pwiz.1.1.mzML
0875f22ba358d4b3224c5a9c96d427c8d5ae9e68c50dd830c533819ab76bde7a  pyteomics-test.mzML
```
