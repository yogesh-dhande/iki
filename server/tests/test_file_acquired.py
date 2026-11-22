"""When a file's data was acquired, read from the file itself (ADR 0177).

Each fixture is built byte by byte from its format's specification, as an instrument
would write it, so the tests don't depend on files that can't be checked in."""

import datetime as dt
import math
import random
import struct
import zlib
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from iki_server.files import acquired
from iki_server.files.acquired import Acquired, BytesSource

MIB = 1024 * 1024

#: An ABIF tag: name, number, element type, element size, data.
AbifTag = tuple[bytes, int, int, int, bytes]
TiffValue = int | bytes | tuple[int, int]
#: A TIFF tag: tag, type, value.
TiffTag = tuple[int, int, TiffValue]
MakeSam = Callable[[Sequence[str | None]], bytes]


class CountingSource:
    """A source that counts what's read of it: calls, bytes asked for, bytes given."""

    def __init__(self, inner: acquired.Readable) -> None:
        self.inner = inner
        self.calls = 0
        self.asked = 0
        self.given = 0

    @property
    def size(self) -> int:
        return self.inner.size

    def read(self, offset: int, length: int) -> bytes:
        data = self.inner.read(offset, length)
        self.calls += 1
        self.asked += length
        self.given += len(data)
        return data


class PaddedSource:
    """A big file that starts with ``prefix`` and is zeros after, without holding them."""

    def __init__(self, prefix: bytes, size: int) -> None:
        self.prefix = prefix
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    def read(self, offset: int, length: int) -> bytes:
        end = min(offset + length, self._size)
        head = self.prefix[offset:end]
        return head + bytes(max(0, end - max(offset, len(self.prefix))))


class ShortSource(BytesSource):
    """A source whose size says more than it has."""

    @property
    def size(self) -> int:
        return super().size + 10_000


class FailingSource:
    size = 1000

    def read(self, offset: int, length: int) -> bytes:
        raise OSError("the store didn't answer")


def read(name: str, data: bytes, media_type: str = "application/octet-stream") -> Acquired | None:
    return acquired.read(name, media_type, BytesSource(data))


# ABIF: the "Applied Biosystems Genetic Analysis Data File Format", big-endian.

ABIF_CHAR, ABIF_SHORT, ABIF_DATE, ABIF_TIME, ABIF_PSTRING = 2, 4, 10, 11, 18


def abif_entry(
    name: bytes, number: int, kind: int, element: int, count: int, size: int, field: bytes
) -> bytes:
    """A 28-byte directory entry. ``field`` is the data offset, or the data itself when
    it's 4 bytes or fewer, left-justified."""
    return (
        struct.pack(">4sihhii", name, number, kind, element, count, size)
        + field.ljust(4, b"\x00")
        + struct.pack(">i", 0)
    )


def abif_date(year: int, month: int, day: int) -> bytes:
    return struct.pack(">HBB", year, month, day)


def abif_time(hour: int, minute: int, second: int, hundredths: int) -> bytes:
    return struct.pack(">BBBB", hour, minute, second, hundredths)


def trace_tags(points: int = 12_500) -> list[AbifTag]:
    """A 3730's analyzed traces (DATA 9 to 12, four dyes), its base calls and peaks:
    about 100 KB, as a 700-base read has."""
    tags = []
    for channel in range(4):
        values = [
            int(800 * math.exp(-(((i + channel * 3) % 12 - 6) ** 2) / 4)) for i in range(points)
        ]
        tags.append((b"DATA", 9 + channel, ABIF_SHORT, 2, struct.pack(f">{points}h", *values)))
    bases = b"".join(b"ACGT"[(i * 7) % 4 : (i * 7) % 4 + 1] for i in range(700))
    tags.append((b"PBAS", 1, ABIF_CHAR, 1, bases))
    tags.append((b"PLOC", 1, ABIF_SHORT, 2, struct.pack(">700h", *range(12, 12 + 700 * 17, 17))))
    return tags


def run_tags(
    start_date: tuple[int, int, int] | None = (2026, 3, 14),
    start_time: tuple[int, int, int, int] | None = (9, 21, 7, 25),
) -> list[AbifTag]:
    """RUND and RUNT: 1 the run's start, 2 its end, 3 and 4 the data collection's. Number
    2 comes first here, so a reader that takes the first RUND it sees is wrong."""
    tags = [
        (b"RUND", 2, ABIF_DATE, 4, abif_date(2026, 3, 15)),
        (b"RUNT", 2, ABIF_TIME, 4, abif_time(11, 2, 44, 0)),
        (b"RUND", 3, ABIF_DATE, 4, abif_date(2026, 3, 14)),
        (b"RUND", 4, ABIF_DATE, 4, abif_date(2026, 3, 14)),
        (b"RUNT", 3, ABIF_TIME, 4, abif_time(9, 40, 0, 0)),
        (b"RUNT", 4, ABIF_TIME, 4, abif_time(10, 55, 0, 0)),
    ]
    if start_date is not None:
        tags.append((b"RUND", 1, ABIF_DATE, 4, abif_date(*start_date)))
    if start_time is not None:
        tags.append((b"RUNT", 1, ABIF_TIME, 4, abif_time(*start_time)))
    return tags


def make_ab1(tags: list[AbifTag], padding: int = 0) -> bytes:
    """An AB1 file: the 128-byte header, the tags' data, ``padding`` more trace bytes,
    then the directory at the end, where the 3730 puts it."""
    body = bytearray(128)
    entries = []
    for name, number, kind, element, data in tags:
        if len(data) <= 4:
            field = data
        else:
            field = struct.pack(">i", len(body))
            body += data
        entries.append(
            abif_entry(name, number, kind, element, len(data) // element, len(data), field)
        )
    body += bytes(padding)
    directory = len(body)
    body += b"".join(entries)
    root = abif_entry(
        b"tdir", 1, 1023, 28, len(entries), 28 * len(entries), struct.pack(">i", directory)
    )
    # 47 reserved int16s fill the header out to 128 bytes.
    header = b"ABIF" + struct.pack(">h", 101) + root + b"\xff" * 94
    body[:128] = header
    return bytes(body)


def realistic_ab1(**run: Any) -> bytes:
    tags = [
        *trace_tags(),
        (b"MODL", 1, ABIF_CHAR, 1, b"3730"),
        (b"SMPL", 1, ABIF_PSTRING, 1, b"\x0aclone-17-F"),
        *run_tags(**run),
    ]
    return make_ab1(tags)


def test_abif_reads_the_run_start() -> None:
    data = realistic_ab1()
    assert len(data) > 100_000
    assert read("clone-17-F.ab1", data) == Acquired(
        on=dt.date(2026, 3, 14), at="2026-03-14T09:21:07.25", source="abif"
    )


def test_abif_gives_hundredths_as_recorded() -> None:
    found = read("a.ab1", realistic_ab1(start_time=(23, 59, 59, 0)))
    assert found is not None
    assert found.at == "2026-03-14T23:59:59.00"


def test_abif_without_a_run_start_time_is_a_date() -> None:
    found = read("a.ab1", realistic_ab1(start_time=None))
    assert found == Acquired(on=dt.date(2026, 3, 14), at="2026-03-14", source="abif")


@pytest.mark.parametrize(
    "run",
    [
        {"start_date": None},
        {"start_date": (1979, 12, 31)},
        {"start_date": (2201, 1, 1)},
        {"start_date": (2026, 2, 30)},
        {"start_date": (2026, 13, 1)},
        {"start_date": (0, 0, 0)},
        {"start_time": (24, 0, 0, 0)},
        {"start_time": (9, 60, 0, 0)},
        {"start_time": (9, 21, 60, 0)},
        {"start_time": (9, 21, 7, 100)},
    ],
)
def test_abif_without_a_valid_run_start_is_none(run: dict[str, Any]) -> None:
    assert read("a.ab1", realistic_ab1(**run)) is None


def test_abif_accepts_the_years_at_the_edges() -> None:
    assert read("a.ab1", realistic_ab1(start_date=(1980, 1, 1), start_time=None)) is not None
    assert read("a.ab1", realistic_ab1(start_date=(2200, 12, 31), start_time=None)) is not None


def test_abif_with_a_wrongly_typed_run_start_is_none() -> None:
    tags = [*trace_tags(100), (b"RUND", 1, ABIF_CHAR, 1, b"2026")]
    assert read("a.ab1", make_ab1(tags)) is None


def test_abif_with_the_wrong_magic_is_none() -> None:
    assert read("a.ab1", b"ABIX" + realistic_ab1()[4:]) is None


def test_abif_reads_little_of_a_big_trace() -> None:
    data = make_ab1([*trace_tags(), *run_tags()], padding=50 * MIB)
    source = CountingSource(BytesSource(data))
    found = acquired.read("big.ab1", "", source)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07.25"
    assert source.calls <= 2
    assert source.asked <= 512 * 1024


# FCS: ISAC's Flow Cytometry Standard 2.0 to 3.2.


def fcs_text(pairs: list[tuple[str, str]], delimiter: str = "/") -> bytes:
    """A TEXT segment: the delimiter, then keyword and value, each followed by it. The
    delimiter in a keyword or value is doubled."""

    def escaped(text: str) -> str:
        return text.replace(delimiter, delimiter * 2)

    return (
        delimiter + "".join(f"{escaped(k)}{delimiter}{escaped(v)}{delimiter}" for k, v in pairs)
    ).encode()


def make_fcs(
    version: str,
    pairs: list[tuple[str, str]],
    data: bytes = b"",
    delimiter: str = "/",
    text_start: int = 58,
) -> bytes:
    """An FCS file: the HEADER (version, four spaces, then offsets as right-justified
    ASCII, ends inclusive), TEXT, then DATA. $BEGINDATA and $ENDDATA, when given as
    placeholders, are filled in, at a fixed width so TEXT's length doesn't change."""
    text = fcs_text(pairs, delimiter)
    text_end = text_start + len(text) - 1
    data_start = text_end + 1
    data_end = data_start + len(data) - 1 if data else 0
    pairs = [
        (
            k,
            f"{data_start:08d}"
            if k == "$BEGINDATA"
            else f"{data_end:08d}"
            if k == "$ENDDATA"
            else v,
        )
        for k, v in pairs
    ]
    text = fcs_text(pairs, delimiter)
    assert len(text) == text_end - text_start + 1
    offsets = (text_start, text_end, data_start, data_end, 0, 0)
    header = version.encode() + b"    " + b"".join(f"{n:>8}".encode() for n in offsets)
    return header.ljust(text_start, b" ") + text + data


def canto_pairs(
    date: str | None = "14-MAR-2026",
    btim: str | None = "09:21:07.25",
    extra: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """What a BD FACSCanto II writes for three parameters and 1,000 events."""
    pairs = [
        ("$BEGINANALYSIS", "0"),
        ("$ENDANALYSIS", "0"),
        ("$BEGINSTEXT", "0"),
        ("$ENDSTEXT", "0"),
        ("$BEGINDATA", "00000000"),
        ("$ENDDATA", "00000000"),
        ("$BYTEORD", "1,2,3,4"),
        ("$DATATYPE", "F"),
        ("$MODE", "L"),
        ("$NEXTDATA", "0"),
        ("$TOT", "1000"),
        ("$PAR", "3"),
        ("$P1N", "FSC-A"),
        ("$P1B", "32"),
        ("$P1E", "0,0"),
        ("$P1R", "262144"),
        ("$P2N", "SSC-A"),
        ("$P2B", "32"),
        ("$P2E", "0,0"),
        ("$P2R", "262144"),
        ("$P3N", "FITC-A"),
        ("$P3S", "CD3"),
        ("$P3B", "32"),
        ("$P3E", "0,0"),
        ("$P3R", "262144"),
        ("$CYT", "FACSCanto II"),
        ("$CYTSN", "V96300123"),
        ("$SRC", "PBMC donor 7"),
        ("$FIL", "Specimen_001_Tube_003.fcs"),
        ("$OP", "jdoe"),
    ]
    if date is not None:
        pairs.append(("$DATE", date))
    if btim is not None:
        pairs.append(("$BTIM", btim))
    pairs.append(("$ETIM", "09:24:51.80"))
    pairs.extend(extra or [])
    return pairs


def canto_events(count: int = 1000) -> bytes:
    values = []
    for i in range(count):
        values += [50_000 + (i * 37) % 9000, 20_000 + (i * 53) % 7000, 100 + (i * 11) % 400]
    return struct.pack(f"<{len(values)}f", *values)


def test_fcs_31_reads_date_and_begin_time() -> None:
    data = make_fcs("FCS3.1", canto_pairs(), canto_events())
    assert read("Specimen_001_Tube_003.fcs", data) == Acquired(
        on=dt.date(2026, 3, 14), at="2026-03-14T09:21:07.25", source="fcs"
    )


def test_fcs_keywords_are_case_insensitive_and_months_too() -> None:
    pairs = [(k.lower(), v) for k, v in canto_pairs(date="04-mar-2026", btim="09:21:07")]
    found = read("a.fcs", make_fcs("FCS3.0", pairs, canto_events(10)))
    assert found is not None
    assert found.at == "2026-03-04T09:21:07"


def test_fcs_32_prefers_begin_date_time_with_its_offset() -> None:
    pairs = canto_pairs(
        date="13-MAR-2026", extra=[("$BEGINDATETIME", "2026-03-14T09:21:07.25+01:00")]
    )
    found = read("a.fcs", make_fcs("FCS3.2", pairs, canto_events(10)))
    assert found == Acquired(
        on=dt.date(2026, 3, 14), at="2026-03-14T09:21:07.25+01:00", source="fcs"
    )


def test_fcs_32_begin_date_time_keeps_z_and_no_offset_as_given() -> None:
    for stamp, at in [
        ("2026-03-14T09:21:07Z", "2026-03-14T09:21:07Z"),
        ("2026-03-14T09:21:07", "2026-03-14T09:21:07"),
        ("2026-03-14T09:21:07-0500", "2026-03-14T09:21:07-05:00"),
        ("2026-03-14T09:21:07-00:00", "2026-03-14T09:21:07"),
    ]:
        pairs = canto_pairs(date=None, btim=None, extra=[("$BEGINDATETIME", stamp)])
        found = read("a.fcs", make_fcs("FCS3.2", pairs))
        assert found is not None, stamp
        assert found.at == at


def test_fcs_reads_past_an_escaped_delimiter() -> None:
    """A value with the delimiter in it, even one that reads like another keyword, is
    one value: the doubled delimiter is the delimiter itself."""
    pairs = canto_pairs(
        extra=[
            ("$COM", "re-acquired; was $DATE/01-JAN-1999/ on the Aria/"),
            ("$CYTOPT", "Cytek/Aurora"),
        ]
    )
    pairs.insert(0, ("$SYS", "Windows/7/"))
    data = make_fcs("FCS3.1", pairs, canto_events(10))
    assert b"$DATE//01-JAN-1999//" in data
    found = read("a.fcs", data)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07.25"


def test_fcs_reads_another_delimiter() -> None:
    data = make_fcs("FCS3.1", canto_pairs(), canto_events(10), delimiter="\x0c")
    found = read("a.fcs", data)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07.25"


def test_fcs_20_sixtieths_are_dropped() -> None:
    data = make_fcs("FCS2.0", canto_pairs(date="14-Mar-2026", btim="09:21:07:30"))
    found = read("a.fcs", data)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07"


def test_fcs_20_two_digit_years_are_none() -> None:
    """Which century 14-Mar-26 means is a guess."""
    assert read("a.fcs", make_fcs("FCS2.0", canto_pairs(date="14-Mar-26"))) is None


def test_fcs_date_without_begin_time_is_a_date() -> None:
    found = read("a.fcs", make_fcs("FCS3.1", canto_pairs(btim=None)))
    assert found == Acquired(on=dt.date(2026, 3, 14), at="2026-03-14", source="fcs")


@pytest.mark.parametrize(
    "pairs",
    [
        canto_pairs(date=None),
        canto_pairs(date="2026-03-14"),
        canto_pairs(date="14-MRZ-2026"),
        canto_pairs(date="31-APR-2026"),
        canto_pairs(date="14-MAR-1970"),
        canto_pairs(btim="9:21"),
        canto_pairs(btim="25:00:00"),
        canto_pairs(btim="09:21:07:75"),
        canto_pairs(extra=[("$DATE", "15-MAR-2026")]),
        canto_pairs(extra=[("$BEGINDATETIME", "14/03/2026 09:21")]),
        canto_pairs(extra=[("$BEGINDATETIME", "2026-03-14T09:21:07+15:00")]),
    ],
)
def test_fcs_without_a_clear_date_is_none(pairs: list[tuple[str, str]]) -> None:
    assert read("a.fcs", make_fcs("FCS3.1", pairs)) is None


def test_fcs_with_another_version_or_bad_offsets_is_none() -> None:
    data = make_fcs("FCS3.1", canto_pairs())
    assert read("a.fcs", b"FCS4.0" + data[6:]) is None
    assert read("a.fcs", data[:10] + b"     abc" + data[18:]) is None
    assert read("a.fcs", data[:10] + b"      10" + data[18:]) is None


def test_fcs_reads_little_of_a_big_file() -> None:
    data = make_fcs("FCS3.1", canto_pairs(), canto_events())
    source = CountingSource(PaddedSource(data, 50 * MIB))
    found = acquired.read("big.fcs", "", source)
    assert found is not None
    assert source.calls == 1
    assert source.asked <= 256 * 1024


# mzML: HUPO-PSI mzML 1.1, in indexedmzML.


def make_mzml(
    run: str = 'id="QC_HeLa_01" defaultInstrumentConfigurationRef="IC1"',
    before: str = "",
    spectra: int = 20,
) -> bytes:
    """An indexed mzML file as msconvert writes it, up to a few spectra."""
    spectrum = (
        '<spectrum index="{i}" id="controllerType=0 controllerNumber=1 scan={n}" '
        'defaultArrayLength="4"><cvParam cvRef="MS" accession="MS:1000511" '
        'name="ms level" value="1"/><binaryDataArrayList count="2"><binaryDataArray '
        'encodedLength="44"><cvParam cvRef="MS" accession="MS:1000514" name="m/z array" '
        'unitAccession="MS:1000040"/><binary>AAAAAAAAWUAAAAAAAMBiQAAAAAAAAGlAAAAAAABAb0A='
        "</binary></binaryDataArray></binaryDataArrayList></spectrum>\n"
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<indexedmzML xmlns="http://psi.hupo.org/ms/mzml" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        '  <mzML xmlns="http://psi.hupo.org/ms/mzml" id="QC_HeLa_01" version="1.1.0">\n'
        '    <cvList count="2"><cv id="MS" fullName="Proteomics Standards Initiative Mass '
        'Spectrometry Ontology" version="4.1.30" URI="https://raw.githubusercontent.com/'
        'HUPO-PSI/psi-ms-CV/master/psi-ms.obo"/><cv id="UO" fullName="Unit Ontology" '
        'URI="https://raw.githubusercontent.com/bio-ontology-research-group/unit-ontology/'
        'master/unit.obo"/></cvList>\n'
        '    <fileDescription><fileContent><cvParam cvRef="MS" accession="MS:1000579" '
        'name="MS1 spectrum"/></fileContent><sourceFileList count="1"><sourceFile id="RAW1" '
        'name="QC_HeLa_01.raw" location="file:///D:/data"/></sourceFileList>'
        "</fileDescription>\n"
        '    <instrumentConfigurationList count="1"><instrumentConfiguration id="IC1">'
        '<cvParam cvRef="MS" accession="MS:1003029" name="Orbitrap Eclipse"/>'
        "</instrumentConfiguration></instrumentConfigurationList>\n"
        f"{before}"
        f"    <run {run}>\n"
        f'      <spectrumList count="{spectra}" defaultDataProcessingRef="pwiz">\n'
        + "".join(spectrum.format(i=i, n=i + 1) for i in range(spectra))
    ).encode()


@pytest.mark.parametrize(
    ("stamp", "at"),
    [
        ("2026-03-14T09:21:07Z", "2026-03-14T09:21:07Z"),
        ("2026-03-14T09:21:07", "2026-03-14T09:21:07"),
        ("2026-03-14T09:21:07.123", "2026-03-14T09:21:07.123"),
        ("2026-03-14T09:21:07+01:00", "2026-03-14T09:21:07+01:00"),
    ],
)
def test_mzml_reads_the_runs_start(stamp: str, at: str) -> None:
    data = make_mzml(f'id="QC" startTimeStamp="{stamp}" defaultSourceFileRef="RAW1"')
    assert read("QC_HeLa_01.mzML", data) == Acquired(on=dt.date(2026, 3, 14), at=at, source="mzml")


def test_mzml_reads_single_quotes_and_a_greater_than_in_another_attribute() -> None:
    data = make_mzml("id='a>b' startTimeStamp='2026-03-14T09:21:07Z'")
    found = read("a.mzml", data)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07Z"


def test_mzml_without_a_start_is_none() -> None:
    assert read("a.mzml", make_mzml()) is None


def test_mzml_doesnt_take_another_element_for_the_run() -> None:
    before = '    <runInfo startTimeStamp="2020-01-01T00:00:00Z"/>\n'
    assert read("a.mzml", make_mzml(before=before)) is None


def test_mzml_with_a_date_alone_is_none() -> None:
    """xs:dateTime has a time."""
    assert read("a.mzml", make_mzml('id="QC" startTimeStamp="2026-03-14"')) is None


def test_mzml_with_its_run_past_the_first_mib_is_none() -> None:
    before = "<!--" + "x" * (2 * MIB) + "-->"
    data = make_mzml('id="QC" startTimeStamp="2026-03-14T09:21:07Z"', before=before)
    source = CountingSource(BytesSource(data))
    assert acquired.read("a.mzml", "", source) is None
    assert source.asked <= MIB


def test_mzml_that_isnt_is_none() -> None:
    assert read("a.mzml", b'<run startTimeStamp="2026-03-14T09:21:07Z">') is None


def test_mzml_reads_little_of_a_big_file() -> None:
    data = make_mzml('id="QC" startTimeStamp="2026-03-14T09:21:07Z"')
    source = CountingSource(PaddedSource(data, 50 * MIB))
    assert acquired.read("big.mzML", "", source) is not None
    assert source.calls == 1
    assert source.asked <= MIB


# JCAMP-DX 4.24 and 5.01.


def make_jcamp(date_lines: list[str], points: int = 1800) -> bytes:
    """An FT-IR spectrum, as JCAMP-DX 5.01 (X++(Y..Y)) writes one."""
    header = [
        "##TITLE=Ethyl acetate, neat, ATR",
        "##JCAMP-DX=5.01 $$ written by OPUS",
        "##DATA TYPE=INFRARED SPECTRUM",
        "##DATA CLASS=XYDATA",
        "##ORIGIN=QC lab 2",
        "##OWNER=Example Pharma",
        *date_lines,
        "##SPECTROMETER/DATA SYSTEM=FT-IR",
        "##XUNITS=1/CM",
        "##YUNITS=TRANSMITTANCE",
        "##RESOLUTION=4",
        "##FIRSTX=4000",
        "##LASTX=400.5",
        "##DELTAX=-2",
        "##XFACTOR=1",
        "##YFACTOR=0.0001",
        f"##NPOINTS={points}",
        "##FIRSTY=0.9512",
        "##XYDATA=(X++(Y..Y))",
    ]
    rows = []
    for start in range(0, points, 10):
        ys = " ".join(str(9500 - (start + i) % 700) for i in range(10))
        rows.append(f"{4000 - 2 * start} {ys}")
    return ("\r\n".join([*header, *rows, "##END="]) + "\r\n").encode("ascii")


@pytest.mark.parametrize(
    ("lines", "at"),
    [
        (["##LONG DATE=2026/03/14 09:21:07.2500 +0100"], "2026-03-14T09:21:07.2500+01:00"),
        (["##LONGDATE=2026/03/14 09:21:07"], "2026-03-14T09:21:07"),
        (["##long_date=2026/03/14"], "2026-03-14"),
        (["##Long-Date= 2026/03/14 09:21:07 -0500 $$ the bench PC"], "2026-03-14T09:21:07-05:00"),
        (["##DATE=2026/03/14", "##TIME=09:21:07"], "2026-03-14T09:21:07"),
        (["##DATE=2026/03/14"], "2026-03-14"),
        (
            ["##DATE=26/03/14", "##TIME=09:21:07", "##LONGDATE=2026/03/14 09:21:07"],
            "2026-03-14T09:21:07",
        ),
    ],
)
def test_jcamp_reads_its_date(lines: list[str], at: str) -> None:
    assert read("ethyl-acetate.jdx", make_jcamp(lines)) == Acquired(
        on=dt.date(2026, 3, 14), at=at, source="jcamp-dx"
    )


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["##DATE=26/03/14", "##TIME=09:21:07"],
        ["##DATE=14/03/2026"],
        ["##DATE=2026/03/14", "##TIME=9:21"],
        ["##LONGDATE=2026-03-14T09:21:07"],
        ["##LONGDATE=2026/02/29 09:21:07"],
        ["##LONGDATE=2026/03/14 09:21:07 +1500"],
    ],
)
def test_jcamp_without_a_clear_date_is_none(lines: list[str]) -> None:
    assert read("a.jdx", make_jcamp(lines)) is None


def test_jcamp_doesnt_read_past_its_first_block() -> None:
    data = make_jcamp([]) + b"##TITLE=second\r\n##LONGDATE=2026/03/14\r\n##END=\r\n"
    assert read("a.jdx", data) is None


def test_jcamp_that_doesnt_start_with_a_title_is_none() -> None:
    assert read("a.jdx", b"##LONGDATE=2026/03/14\r\n##TITLE=x\r\n##END=\r\n") is None


def test_jcamp_reads_little_of_a_big_file() -> None:
    data = make_jcamp(["##LONGDATE=2026/03/14 09:21:07"])
    source = CountingSource(PaddedSource(data, 50 * MIB))
    assert acquired.read("big.jdx", "", source) is not None
    assert source.calls == 1
    assert source.asked <= 256 * 1024


# TIFF 6.0, EXIF 2.32 and 3.0, OME-TIFF; JPEG's APP1.

TIFF_SHORT, TIFF_LONG, TIFF_ASCII, TIFF_UNDEFINED, TIFF_RATIONAL = 3, 4, 2, 7, 5


def ascii_tag(tag: int, text: str) -> TiffTag:
    return (tag, TIFF_ASCII, text.encode() + b"\x00")


def short_tag(tag: int, value: int) -> TiffTag:
    return (tag, TIFF_SHORT, value)


def long_tag(tag: int, value: int) -> TiffTag:
    return (tag, TIFF_LONG, value)


def tag_bytes(order: str, kind: int, value: TiffValue) -> tuple[int, bytes]:
    """A tag's count and value, packed in the file's byte order."""
    if isinstance(value, bytes):
        return len(value), value
    if isinstance(value, tuple):
        return 1, struct.pack(order + "II", *value)
    return 1, struct.pack(order + ("H" if kind == TIFF_SHORT else "I"), value)


def pack_ifd(order: str, tags: list[TiffTag], values_at: int) -> tuple[bytes, bytes]:
    """An IFD (its count, 12-byte entries sorted by tag, next IFD 0), and the values too
    big for an entry, from ``values_at``, each at a word boundary."""
    entries = b""
    values = b""
    for tag, kind, value in sorted(tags, key=lambda entry: entry[0]):
        count, data = tag_bytes(order, kind, value)
        if len(data) <= 4:
            field = data.ljust(4, b"\x00")
        else:
            field = struct.pack(order + "I", values_at + len(values))
            values += data + b"\x00" * (len(data) % 2)
        entries += struct.pack(order + "HHI", tag, kind, count) + field
    return struct.pack(order + "H", len(tags)) + entries + struct.pack(order + "I", 0), values


def make_tiff(
    order: str,
    ifd0: list[TiffTag],
    exif: list[TiffTag] | None = None,
    image: bytes | None = None,
    image_first: bool = False,
) -> bytes:
    """A classic TIFF: the header, IFD0, the Exif IFD, their values, and a grayscale
    strip. With ``image_first``, the strip comes before the IFDs, as many writers do."""
    width = 64
    height = len(image) // width if image else 0

    def ifd0_tags(strip_at: int, exif_at: int) -> list[TiffTag]:
        tags = list(ifd0)
        if image is not None:
            tags += [
                long_tag(256, width),
                long_tag(257, height),
                short_tag(258, 8),
                short_tag(259, 1),
                short_tag(262, 1),
                long_tag(273, strip_at),
                short_tag(277, 1),
                long_tag(278, height),
                long_tag(279, len(image)),
            ]
        if exif is not None:
            tags.append(long_tag(0x8769, exif_at))
        return tags

    def layout(strip_at: int) -> tuple[int, bytes]:
        start = 8 + (len(image) if image_first and image else 0)
        ifd0_size = 2 + 12 * len(ifd0_tags(0, 0)) + 4
        exif_at = start + ifd0_size
        exif_size = 2 + 12 * len(exif) + 4 if exif is not None else 0
        values_at = exif_at + exif_size
        first, values0 = pack_ifd(order, ifd0_tags(strip_at, exif_at), values_at)
        second, values1 = (
            pack_ifd(order, exif, values_at + len(values0)) if exif is not None else (b"", b"")
        )
        return start, first + second + values0 + values1

    magic = b"II*\x00" if order == "<" else b"MM\x00*"
    if image_first and image:
        start, ifds = layout(8)
        return magic + struct.pack(order + "I", start) + image + ifds
    start, ifds = layout(0)
    start, ifds = layout(8 + len(ifds))
    return magic + struct.pack(order + "I", start) + ifds + (image or b"")


def camera_ifd0(description: str | None = None) -> list[TiffTag]:
    tags = [
        ascii_tag(0x010F, "OLYMPUS"),
        ascii_tag(0x0110, "DP74"),
        ascii_tag(0x0131, "cellSens 4.2"),
        # When the file was last changed: it's never what's read.
        ascii_tag(0x0132, "2026:03:20 17:45:00"),
        (0x011A, TIFF_RATIONAL, (72, 1)),
    ]
    if description is not None:
        tags.append(ascii_tag(0x010E, description))
    return tags


def camera_exif(
    original: str | None = "2026:03:14 09:21:07",
    subsec: str | None = "25",
    offset: str | None = "+01:00",
) -> list[TiffTag]:
    tags = [
        (0x9000, TIFF_UNDEFINED, b"0232"),
        ascii_tag(0x9004, "2026:03:14 09:21:08"),
        (0x829A, TIFF_RATIONAL, (1, 60)),
    ]
    if original is not None:
        tags.append(ascii_tag(0x9003, original))
    if subsec is not None:
        tags.append(ascii_tag(0x9291, subsec))
    if offset is not None:
        tags.append(ascii_tag(0x9011, offset))
    return tags


def gradient(size: int = 64 * 64) -> bytes:
    return bytes(i % 251 for i in range(size))


@pytest.mark.parametrize("order", ["<", ">"])
def test_tiff_reads_exif_date_time_original(order: str) -> None:
    data = make_tiff(order, camera_ifd0(), camera_exif(), gradient())
    assert data[:2] == (b"II" if order == "<" else b"MM")
    assert read("well-A1.tif", data) == Acquired(
        on=dt.date(2026, 3, 14), at="2026-03-14T09:21:07.25+01:00", source="exif"
    )


def test_tiff_reads_its_ifds_after_the_image() -> None:
    data = make_tiff(">", camera_ifd0(), camera_exif(), gradient(), image_first=True)
    found = read("a.tiff", data)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07.25+01:00"


@pytest.mark.parametrize(
    ("exif", "at"),
    [
        ({"subsec": None, "offset": None}, "2026-03-14T09:21:07"),
        ({"subsec": "  ", "offset": "   :  "}, "2026-03-14T09:21:07"),
        ({"subsec": "007", "offset": None}, "2026-03-14T09:21:07.007"),
        ({"subsec": None, "offset": "-05:30"}, "2026-03-14T09:21:07-05:30"),
        ({"original": "2026:03:14 09:21:07  "}, "2026-03-14T09:21:07.25+01:00"),
    ],
)
def test_tiff_exif_precision_is_as_given(exif: dict[str, str | None], at: str) -> None:
    data = make_tiff("<", camera_ifd0(), camera_exif(**exif), gradient())
    found = read("a.tif", data)
    assert found is not None
    assert found.at == at


@pytest.mark.parametrize(
    "exif",
    [
        {"original": "0000:00:00 00:00:00"},
        {"original": "    :  :     :  :  "},
        {"original": ""},
        {"original": "2026:03:14"},
        {"original": "2026-03-14 09:21:07"},
        {"original": "2026:02:29 09:21:07"},
        {"original": "1970:01:01 00:00:00"},
        {"subsec": "2x"},
        {"offset": "CET"},
    ],
)
def test_tiff_without_a_clear_exif_date_is_none(exif: dict[str, str]) -> None:
    assert read("a.tif", make_tiff("<", camera_ifd0(), camera_exif(**exif), gradient())) is None


def test_tiff_with_only_ifd0_date_time_is_none() -> None:
    """DateTime is when the file was changed, not when the image was taken."""
    assert read("a.tif", make_tiff("<", camera_ifd0(), None, gradient())) is None
    assert (
        read("a.tif", make_tiff("<", camera_ifd0(), camera_exif(original=None), gradient())) is None
    )


def test_bigtiff_is_none() -> None:
    data = make_tiff("<", camera_ifd0(), camera_exif(), gradient())
    assert read("a.tif", b"II+\x00" + data[4:]) is None


def ome_xml(acquisition: str | None = "2026-03-14T09:21:07.250") -> str:
    date = f"<AcquisitionDate>{acquisition}</AcquisitionDate>" if acquisition else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'UUID="urn:uuid:5f1c0e2a-1d2b-4c3e-9f8a-7b6c5d4e3f21" Creator="OME Bio-Formats 8.1.0">'
        '<Instrument ID="Instrument:0"><Objective ID="Objective:0:0" NominalMagnification="20"/>'
        "</Instrument>"
        f'<Image ID="Image:0" Name="well A1, field 1">{date}'
        '<Pixels BigEndian="false" DimensionOrder="XYCZT" ID="Pixels:0" SizeC="1" SizeT="1" '
        'SizeX="64" SizeY="64" SizeZ="1" Type="uint8"><Channel ID="Channel:0:0" '
        'SamplesPerPixel="1"/><TiffData IFD="0" PlaneCount="1"/></Pixels></Image>'
        '<Image ID="Image:1" Name="well A1, field 2">'
        "<AcquisitionDate>2026-03-14T09:30:00</AcquisitionDate></Image></OME>"
    )


def test_ome_tiff_reads_the_first_images_acquisition_date() -> None:
    data = make_tiff("<", camera_ifd0(ome_xml()), None, gradient())
    assert read("plate.ome.tif", data) == Acquired(
        on=dt.date(2026, 3, 14), at="2026-03-14T09:21:07.250", source="ome-tiff"
    )


def test_ome_tiff_comes_before_exif() -> None:
    data = make_tiff(">", camera_ifd0(ome_xml()), camera_exif(original="2025:01:01 00:00:00"))
    found = read("plate.ome.tiff", data)
    assert found is not None
    assert found.source == "ome-tiff"


def test_ome_tiff_without_an_acquisition_date_falls_back_to_exif() -> None:
    xml = ome_xml(acquisition=None).replace(
        "<AcquisitionDate>2026-03-14T09:30:00</AcquisitionDate>", ""
    )
    data = make_tiff("<", camera_ifd0(xml), camera_exif(), gradient())
    found = read("a.ome.tif", data)
    assert found is not None
    assert found.source == "exif"


def test_ome_tiff_with_a_malformed_acquisition_date_is_none() -> None:
    data = make_tiff("<", camera_ifd0(ome_xml("14 March 2026")), camera_exif(), gradient())
    assert read("a.ome.tif", data) is None


def test_a_description_that_isnt_ome_xml_isnt_read() -> None:
    description = "<AcquisitionDate>2020-01-01T00:00:00</AcquisitionDate>"
    data = make_tiff("<", camera_ifd0(description), camera_exif(), gradient())
    found = read("a.tif", data)
    assert found is not None
    assert found.source == "exif"


def segment(marker: int, body: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body


def make_jpeg(exif_tiff: bytes | None, xmp: bool = False) -> bytes:
    """A baseline JPEG: SOI, JFIF's APP0, maybe XMP's APP1, Exif's APP1, then a table, a
    frame, a scan and EOI."""
    parts = [b"\xff\xd8", segment(0xE0, b"JFIF\x00\x01\x02\x00\x00\x48\x00\x48\x00\x00")]
    if xmp:
        packet = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'
        parts.append(segment(0xE1, b"http://ns.adobe.com/xap/1.0/\x00" + packet))
    if exif_tiff is not None:
        parts.append(segment(0xE1, b"Exif\x00\x00" + exif_tiff))
    parts += [
        segment(0xDB, b"\x00" + bytes(range(1, 65))),
        segment(0xC0, b"\x08\x00\x10\x00\x10\x01\x01\x11\x00"),
        segment(
            0xC4,
            b"\x00" + bytes([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0]) + bytes(range(12)),
        ),
        segment(0xDA, b"\x01\x01\x00\x00\x3f\x00"),
        bytes((i * 13) % 255 for i in range(2048)),
        b"\xff\xd9",
    ]
    return b"".join(parts)


@pytest.mark.parametrize("order", ["<", ">"])
def test_jpeg_reads_exif_after_app0(order: str) -> None:
    data = make_jpeg(make_tiff(order, camera_ifd0(), camera_exif()))
    assert read("gel.jpg", data) == Acquired(
        on=dt.date(2026, 3, 14), at="2026-03-14T09:21:07.25+01:00", source="exif"
    )


def test_jpeg_reads_exif_after_xmp() -> None:
    data = make_jpeg(make_tiff("<", camera_ifd0(), camera_exif(subsec=None)), xmp=True)
    found = read("gel.jpeg", data)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07+01:00"


def test_jpeg_without_exif_is_none() -> None:
    assert read("gel.jpg", make_jpeg(None, xmp=True)) is None
    assert read("gel.jpg", make_jpeg(make_tiff("<", camera_ifd0(), None))) is None


@pytest.mark.parametrize(
    ("name", "make"),
    [
        ("big.tif", lambda: make_tiff("<", camera_ifd0(), camera_exif(), gradient())),
        ("big.jpg", lambda: make_jpeg(make_tiff(">", camera_ifd0(), camera_exif()))),
    ],
)
def test_images_read_little_of_a_big_file(name: str, make: Callable[[], bytes]) -> None:
    source = CountingSource(PaddedSource(make(), 50 * MIB))
    assert acquired.read(name, "", source) is not None
    assert source.calls == 1
    assert source.asked <= 256 * 1024


def test_tiff_with_its_ifds_at_the_end_of_a_big_file_reads_little() -> None:
    data = make_tiff("<", camera_ifd0(), camera_exif(), bytes(50 * MIB), image_first=True)
    source = CountingSource(BytesSource(data))
    found = acquired.read("big.tif", "", source)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07.25+01:00"
    assert source.calls <= 2
    assert source.asked <= 512 * 1024


# SAM and BAM: the SAM/BAM Format Specification (v1.6).


def sam_header(read_group_dates: Sequence[str | None], contigs: int = 3) -> str:
    lines = ["@HD\tVN:1.6\tSO:coordinate"]
    lines += [f"@SQ\tSN:chr{i + 1}\tLN:{248_956_422 - i * 1_000_003}" for i in range(contigs)]
    for i, date in enumerate(read_group_dates):
        fields = [f"@RG\tID:HV7KCDSX5.{i + 1}", "SM:NA12878", "LB:lib1", "PL:ILLUMINA"]
        if date is not None:
            fields.append(f"DT:{date}")
        fields.append("PU:HV7KCDSX5.1.ACGTACGT")
        lines.append("\t".join(fields))
    lines.append("@PG\tID:bwa\tPN:bwa\tVN:0.7.18\tCL:bwa mem ref.fa r1.fq r2.fq")
    return "\n".join(lines) + "\n"


SAM_RECORDS = (
    "r001\t99\tchr1\t7\t30\t8M2I4M1D3M\t=\t37\t39\tTTAGATAAAGGATACTG\t*\n"
    "r002\t0\tchr1\t9\t30\t3S6M1P1I4M\t*\t0\t0\tAAAAGATAAGGATA\t*\n"
)


def make_sam(read_group_dates: Sequence[str | None], contigs: int = 3) -> bytes:
    return (sam_header(read_group_dates, contigs) + SAM_RECORDS).encode()


def bgzf_block(data: bytes) -> bytes:
    """A BGZF block: a gzip member whose extra field's BC subfield gives its size."""
    deflater = zlib.compressobj(6, zlib.DEFLATED, -15)
    deflated = deflater.compress(data) + deflater.flush()
    size = 18 + len(deflated) + 8
    header = (
        b"\x1f\x8b\x08\x04"
        + b"\x00\x00\x00\x00"
        + b"\x00\xff"
        + struct.pack("<H", 6)
        + b"BC"
        + struct.pack("<HH", 2, size - 1)
    )
    return header + deflated + struct.pack("<II", zlib.crc32(data), len(data))


BGZF_EOF = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")


def bgzf(data: bytes, block: int = 65_280) -> bytes:
    return b"".join(bgzf_block(data[i : i + block]) for i in range(0, len(data), block)) + BGZF_EOF


def make_bam(read_group_dates: Sequence[str | None], contigs: int = 3) -> bytes:
    """A BAM file: the magic, the header text, the references, one alignment, in BGZF."""
    text = sam_header(read_group_dates, contigs).encode()
    raw = b"BAM\x01" + struct.pack("<i", len(text)) + text + struct.pack("<i", contigs)
    for i in range(contigs):
        name = f"chr{i + 1}".encode() + b"\x00"
        raw += struct.pack("<i", len(name)) + name + struct.pack("<i", 248_956_422)
    name = b"r001\x00"
    sequence = bytes([0x18, 0x42, 0x11, 0x88])
    record = (
        struct.pack("<iiBBHHHi", 0, 6, len(name), 30, 4681, 1, 99, 8)
        + struct.pack("<iii", 0, 36, 39)
        + name
        + struct.pack("<I", 8 << 4)
        + sequence
        + bytes([30] * 8)
    )
    raw += struct.pack("<i", len(record)) + record
    return bgzf(raw)


DATES = ["2026-03-15T10:00:00", "2026-03-14T09:21:07", "2026-03-16"]


@pytest.mark.parametrize("make", [make_sam, make_bam])
def test_sam_and_bam_read_the_earliest_read_group_date(make: MakeSam) -> None:
    data = make(DATES)
    found = acquired.read(
        "NA12878." + ("sam" if make is make_sam else "bam"), "", BytesSource(data)
    )
    assert found == Acquired(on=dt.date(2026, 3, 14), at="2026-03-14T09:21:07", source="sam")


def test_bam_reads_a_header_over_several_blocks() -> None:
    data = make_bam(DATES, contigs=3000)
    assert data.count(b"BC") > 2
    found = read("a.bam", data)
    assert found is not None
    assert found.at == "2026-03-14T09:21:07"


def test_read_groups_with_offsets_are_compared_as_instants() -> None:
    data = make_sam(["2026-03-14T09:00:00+01:00", "2026-03-14T08:30:00Z"])
    found = read("a.sam", data)
    assert found is not None
    assert found.at == "2026-03-14T09:00:00+01:00"


def test_read_groups_are_dates_when_they_say_so() -> None:
    found = read("a.sam", make_sam(["2026-03-14", None]))
    assert found == Acquired(on=dt.date(2026, 3, 14), at="2026-03-14", source="sam")


@pytest.mark.parametrize(
    "dates",
    [
        [],
        [None, None],
        ["2026-03-14T09:21:07", "14 March 2026"],
        ["1970-01-01"],
        ["20260314"],
    ],
)
def test_sam_without_clear_read_group_dates_is_none(dates: list[str | None]) -> None:
    assert read("a.sam", make_sam(dates)) is None
    assert read("a.bam", make_bam(dates)) is None


def test_sam_only_reads_its_header() -> None:
    data = (
        make_sam([])
        + b"r003\t0\tchr1\t9\t30\t5M\t*\t0\t0\tAAAAA\t*\tRG:Z:x\n@RG\tID:x\tDT:2026-03-14\n"
    )
    assert read("a.sam", data) is None


@pytest.mark.parametrize(("name", "make"), [("big.sam", make_sam), ("big.bam", make_bam)])
def test_sam_and_bam_read_little_of_a_big_file(name: str, make: MakeSam) -> None:
    source = CountingSource(PaddedSource(make(DATES), 50 * MIB))
    assert acquired.read(name, "", source) is not None
    assert source.calls == 1
    assert source.asked <= MIB


def test_a_header_past_the_budget_is_none() -> None:
    """A SAM header this long might name an earlier date past what's read."""
    data = make_sam(DATES, contigs=200_000)
    assert len(data) > 4 * MIB
    source = CountingSource(BytesSource(data))
    assert acquired.read("a.sam", "", source) is None
    assert source.asked <= 4 * MIB


def test_a_bam_header_past_the_budget_is_none() -> None:
    raw = b"BAM\x01" + struct.pack("<i", 8 * MIB) + bytes(8 * MIB)
    source = CountingSource(BytesSource(bgzf(raw)))
    assert acquired.read("a.bam", "", source) is None
    assert source.asked <= 4 * MIB


# Choosing a reader.


def fixtures() -> dict[str, tuple[str, bytes]]:
    """One of each, small: what the fuzzing below cuts and corrupts."""
    return {
        "abif": ("a.ab1", make_ab1([*trace_tags(200), *run_tags()])),
        "fcs": ("a.fcs", make_fcs("FCS3.1", canto_pairs(), canto_events(20))),
        "fcs-3.2": (
            "a.fcs",
            make_fcs("FCS3.2", canto_pairs(extra=[("$BEGINDATETIME", "2026-03-14T09:21:07Z")])),
        ),
        "mzml": ("a.mzml", make_mzml('id="QC" startTimeStamp="2026-03-14T09:21:07Z"', spectra=3)),
        "jcamp-dx": ("a.jdx", make_jcamp(["##LONGDATE=2026/03/14 09:21:07 +0100"], points=100)),
        "exif-tiff": ("a.tif", make_tiff(">", camera_ifd0(), camera_exif(), gradient(256))),
        "exif-jpeg": ("a.jpg", make_jpeg(make_tiff("<", camera_ifd0(), camera_exif()), xmp=True)),
        "ome-tiff": ("a.ome.tif", make_tiff("<", camera_ifd0(ome_xml()), None, gradient(256))),
        "sam": ("a.sam", make_sam(DATES)),
        "bam": ("a.bam", make_bam(DATES)),
    }


def test_every_source_is_named_for_people() -> None:
    found = [read(name, data) for name, data in fixtures().values()]
    assert {each.source for each in found if each is not None} == set(acquired.SOURCES)
    assert None not in found
    assert all(phrase.startswith("the ") for phrase in acquired.SOURCES.values())


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("trace.AB1", "application/octet-stream"),
        ("trace.Abi", ""),
        ("trace.fsa", ""),
        ("upload", "application/x-abi"),
        ("upload.bin", "application/x-abi; charset=binary"),
        ("upload.bin", "Application/X-ABI"),
    ],
)
def test_a_reader_is_chosen_by_extension_or_media_type(name: str, media_type: str) -> None:
    assert acquired.read(name, media_type, BytesSource(realistic_ab1())) is not None


def test_the_extension_comes_before_the_media_type() -> None:
    data = realistic_ab1()
    assert acquired.read("trace.fcs", "application/x-abi", BytesSource(data)) is None
    assert acquired.read("trace.ab1", "application/vnd.isac.fcs", BytesSource(data)) is not None


@pytest.mark.parametrize(
    ("media_type", "name", "data"),
    [
        ("application/vnd.isac.fcs", "x", lambda: make_fcs("FCS3.1", canto_pairs())),
        ("application/x-mzml", "x", lambda: make_mzml('startTimeStamp="2026-03-14T09:21:07Z"')),
        ("chemical/x-jcamp-dx", "x", lambda: make_jcamp(["##LONGDATE=2026/03/14"])),
        ("image/tiff", "x", lambda: make_tiff("<", camera_ifd0(), camera_exif(), gradient())),
        ("image/jpeg", "x", lambda: make_jpeg(make_tiff("<", camera_ifd0(), camera_exif()))),
        ("text/x-sam", "x", lambda: make_sam(DATES)),
        ("application/x-bam", "x", lambda: make_bam(DATES)),
    ],
)
def test_each_media_type_has_its_reader(
    media_type: str, name: str, data: Callable[[], bytes]
) -> None:
    assert acquired.read(name, media_type, BytesSource(data())) is not None


def test_other_files_are_none() -> None:
    data = realistic_ab1()
    assert read("trace.txt", data, "text/plain") is None
    assert read("trace", data, "") is None
    assert read("notes.tif", b"just some notes") is None
    assert read("empty.ab1", b"") is None


def test_a_jpeg_named_tiff_is_still_read() -> None:
    data = make_jpeg(make_tiff("<", camera_ifd0(), camera_exif()))
    found = read("gel.tif", data)
    assert found is not None
    assert found.source == "exif"


def test_a_source_shorter_than_its_size_never_raises() -> None:
    for name, data in fixtures().values():
        found = acquired.read(name, "", ShortSource(data[: len(data) // 2]))
        assert found is None or found.at.startswith(found.on.isoformat()), name
    # An AB1's directory is at its end, which this one doesn't have.
    assert acquired.read("a.ab1", "", ShortSource(realistic_ab1()[:-100])) is None


def test_a_failing_source_raises_so_the_caller_can_try_again() -> None:
    with pytest.raises(OSError, match="didn't answer"):
        acquired.read("a.ab1", "", FailingSource())


def test_at_always_starts_with_on() -> None:
    for name, data in fixtures().values():
        found = read(name, data)
        assert found is not None, name
        assert found.at.startswith(found.on.isoformat())


@pytest.mark.parametrize("fixture", list(fixtures()))
def test_cut_or_corrupted_files_never_raise(fixture: str) -> None:
    name, data = fixtures()[fixture]
    cuts = sorted({*range(min(len(data), 600)), *range(0, len(data), max(1, len(data) // 300))})
    for cut in cuts:
        found = read(name, data[:cut])
        assert found is None or found.at.startswith(found.on.isoformat())
    randomly = random.Random(fixture)  # noqa: S311 - made-up corruption, not secrets
    for _ in range(300):
        corrupted = bytearray(data)
        for _ in range(randomly.randint(1, 8)):
            corrupted[randomly.randrange(len(corrupted))] = randomly.randrange(256)
        found = read(name, bytes(corrupted))
        assert found is None or found.at.startswith(found.on.isoformat())
