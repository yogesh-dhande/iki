"""Exercise parser edge cases with explicitly synthetic FASTQ records."""

import gzip
import runpy
from pathlib import Path

import pytest

statistics = runpy.run_path(str(Path(__file__).parents[1] / "scripts/fastq.py"))["statistics"]


def test_wrapped_gzip_and_quality_boundaries(tmp_path):
    # Cock et al., https://doi.org/10.1093/nar/gkp1137, Table 1, Phred+33.
    # ASCII ! and ~ are Q0/Q93. > and ? straddle Q30 at Q29 and Q30.
    path = tmp_path / "synthetic.fastq.gz"
    path.write_bytes(gzip.compress(b"@first\r\nAC\r\nGT\r\n+first\r\n!>\r\n?~\r\n"))
    assert statistics(path) == [
        {
            "reads": 1,
            "bases": 4,
            "min_length": 4,
            "max_length": 4,
            "mean_phred": 38.0,
            "q30_percent": 50.0,
        }
    ]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "contains no reads"),
        (b"@read\nAC\n+\n!\n", "expected printable"),
        (b"@read\nA\n+\n!!\n", "counts differ"),
        (b"@read\nA\n+different\n!\n", "title differs"),
        (b"@read\nA X\n+\n!!!\n", "expected sequence bases"),
    ],
)
def test_refuses_invalid_fastq(tmp_path, data, message):
    path = tmp_path / "synthetic.fastq"
    path.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        statistics(path)


@pytest.mark.parametrize(
    ("second", "message"),
    [
        (b"@another/2\nG\n+\n!\n", "identifiers differ at read 1"),
        (b"", "different read counts"),
        (b"@read/1\nG\n+\n!\n", "marks it mate 1: the two files may be swapped"),
    ],
)
def test_refuses_unsynchronized_pairs(tmp_path, second, message):
    first_path, second_path = (tmp_path / f"synthetic_{mate}.fastq" for mate in (1, 2))
    first_path.write_bytes(b"@read/1\nA\n+\n!\n")
    second_path.write_bytes(second)
    with pytest.raises(ValueError, match=message):
        statistics(first_path, second_path)


DATA = Path(__file__).parent / "data"


def test_the_archived_pair_swapped_is_refused():
    """ENA keeps its run's read name first and the instrument's title, ending /1 or /2,
    second: https://www.ebi.ac.uk/ena/browser/view/SRR8690267. Swapped, read 2's
    qualities (Q30 14%) were reported as read 1's (90%)."""
    first, second = DATA / "SRR8690267_1.fastq", DATA / "SRR8690267_2.fastq"
    assert [s["q30_percent"] for s in statistics(first, second)] == [90.0, 14.0]
    with pytest.raises(ValueError, match="marks it mate 2: the two files may be swapped"):
        statistics(second, first)


# One read pair's titles in each form a sequencer or an archive writes them.
TITLES = {
    # Illumina before CASAVA 1.8: /1 and /2 end the read name.
    "illumina": (b"HWUSI-EAS100R:6:73:941:1973#0/1", b"HWUSI-EAS100R:6:73:941:1973#0/2"),
    # CASAVA 1.8 and later: the second token starts with the mate number.
    "casava": (
        b"EAS139:136:FC706VJ:2:2104:15343:197393 1:Y:18:ATCACG",
        b"EAS139:136:FC706VJ:2:2104:15343:197393 2:Y:18:ATCACG",
    ),
    # ENA: its read name, then the instrument's title with /1 or /2.
    "ena": (
        b"SRR8690267.1 HWI-D00733:25:C6KC4ANXX:8:1101:1458:2222/1",
        b"SRR8690267.1 HWI-D00733:25:C6KC4ANXX:8:1101:1458:2222/2",
    ),
    # SRA fastq-dump --readids: accession.spot.read.
    "sra_readids": (b"SRR001666.1.1 071112_SLXA-EAS1_s_7:5:1:817:345 length=36",
                    b"SRR001666.1.2 071112_SLXA-EAS1_s_7:5:1:817:345 length=36"),
}  # fmt: skip


def pair(tmp_path, first_title, second_title):
    paths = []
    for mate, title in ((1, first_title), (2, second_title)):
        path = tmp_path / f"synthetic_{mate}.fastq"
        path.write_bytes(b"@" + title + b"\nACGT\n+\nIIII\n")
        paths.append(path)
    return paths


@pytest.mark.parametrize("form", sorted(TITLES))
def test_each_title_form_pairs_in_order(tmp_path, form):
    first, second = TITLES[form]
    assert [s["reads"] for s in statistics(*pair(tmp_path, first, second))] == [1, 1]


@pytest.mark.parametrize("form", sorted(TITLES))
def test_each_title_form_is_refused_swapped(tmp_path, form):
    first, second = TITLES[form]
    with pytest.raises(ValueError, match="the two files may be swapped"):
        statistics(*pair(tmp_path, second, first))


def test_titles_that_name_no_mate_pair_by_their_read_name(tmp_path):
    """fastq-dump without --readids writes the same title in both files: nothing says
    which mate is which, so only the read names are compared."""
    title = b"SRR001666.1 071112_SLXA-EAS1_s_7:5:1:817:345 length=36"
    assert [s["reads"] for s in statistics(*pair(tmp_path, title, title))] == [1, 1]
    with pytest.raises(ValueError, match="identifiers differ at read 1"):
        statistics(*pair(tmp_path, title, b"SRR001666.2 071112_SLXA-EAS1_s_7:5:1:817:345"))
