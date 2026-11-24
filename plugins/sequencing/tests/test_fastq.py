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
        (b"@read/1\nG\n+\n!\n", "wrong mate suffix"),
    ],
)
def test_refuses_unsynchronized_pairs(tmp_path, second, message):
    first_path, second_path = (tmp_path / f"synthetic_{mate}.fastq" for mate in (1, 2))
    first_path.write_bytes(b"@read/1\nA\n+\n!\n")
    second_path.write_bytes(second)
    with pytest.raises(ValueError, match=message):
        statistics(first_path, second_path)
