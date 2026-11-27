"""Read-only FASTQ quality statistics for an Iki sequencing read set."""

from __future__ import annotations

import gzip
import json
import os
import re
import zlib
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any, BinaryIO

# Cock et al. (2010), Table 1, Sanger FASTQ, https://doi.org/10.1093/nar/gkp1137.
PHRED_OFFSET = 33
BELOW_Q30 = bytes(range(PHRED_OFFSET, PHRED_OFFSET + 30))
QUALITY = re.compile(rb"[!-~]+")
BASES = re.compile(rb"[ACGTURYSWKMBDHVNacgturyswkmbdhvn.]+")
MAX_READ_LENGTH = 10_000_000


@dataclass
class Summary:
    reads: int = 0
    bases: int = 0
    min_length: int = MAX_READ_LENGTH
    max_length: int = 0
    quality_sum: int = 0
    q30_bases: int = 0

    def add(self, quality: bytes) -> None:
        length = len(quality)
        self.reads += 1
        self.bases += length
        self.min_length = min(self.min_length, length)
        self.max_length = max(self.max_length, length)
        self.quality_sum += sum(quality) - PHRED_OFFSET * length
        self.q30_bases += len(quality.translate(None, BELOW_Q30))

    def values(self) -> dict[str, int | float]:
        return {
            "reads": self.reads,
            "bases": self.bases,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "mean_phred": self.quality_sum / self.bases,
            "q30_percent": 100 * self.q30_bases / self.bases,
        }


def line(source: BinaryIO) -> bytes:
    raw = source.readline(MAX_READ_LENGTH + 3)
    content = raw.removesuffix(b"\n").removesuffix(b"\r")
    if len(content) > MAX_READ_LENGTH:
        raise ValueError("a FASTQ line exceeds 10 million characters.")
    return content


def read_fastq(source: BinaryIO) -> Iterator[tuple[bytes, bytes]]:
    # Cock et al. permit wrapping and a repeated title after the + delimiter.
    number = 1
    while header := source.readline(MAX_READ_LENGTH + 3):
        try:
            title = header.removesuffix(b"\n").removesuffix(b"\r")
            if len(title) > MAX_READ_LENGTH or not title.startswith(b"@") or not title[1:].split():
                raise ValueError("expected an @ title with a read identifier.")
            bases = 0
            while not (part := line(source)).startswith(b"+"):
                if not BASES.fullmatch(part):
                    raise ValueError("expected sequence bases followed by a + separator.")
                bases += len(part)
                if bases > MAX_READ_LENGTH:
                    raise ValueError("the sequence exceeds 10 million bases.")
            if not bases:
                raise ValueError("the read has no bases.")
            if part[1:] and part[1:] != title[1:]:
                raise ValueError("the + title differs from the @ title.")
            quality = bytearray()
            while len(quality) < bases:
                part = line(source)
                if not QUALITY.fullmatch(part):
                    raise ValueError("expected printable Phred+33 qualities for every base.")
                quality.extend(part)
            if len(quality) != bases:
                raise ValueError("the base and quality counts differ.")
            yield title[1:], bytes(quality)
        except ValueError as exc:
            raise ValueError(f"read {number}: {exc}") from exc
        number += 1


#: Where a FASTQ title says which mate a read is:
#: - a token ending /1 or /2: the first, in Illumina's titles before CASAVA 1.8
#:   (`@HWUSI-EAS100R:6:73:941:1973#0/1`), or a later one, where ENA keeps the
#:   instrument's title after the run's own read name
#:   (`@SRR8690267.1 HWI-D00733:25:C6KC4ANXX:8:1101:1458:2222/1`);
#: - CASAVA 1.8's second token, `1:N:0:ATCACG`: mate, filtered, control bits, index
#:   (Illumina, "FASTQ files explained");
#: - SRA's `fastq-dump --readids`, which appends the read to the spot as
#:   `accession.spot.read` (`@SRR001666.1.2`).
#: A title with none of them, such as `fastq-dump`'s default `@SRR001666.1`, says nothing.
SLASH_MATE = re.compile(rb".*/([12])")
CASAVA_MATE = re.compile(rb"([12]):[YN]:\d+:.*")
SRA_READ_ID = re.compile(rb"([^.\s]+\.\d+)\.([12])")


def mate_id(title: bytes, mate: int) -> bytes:
    """The read's identifier, without its mate, refusing a title that names the other
    mate: the files would be swapped, and each mate's quality reported as the other's."""
    identifier, *rest = title.split()
    named: set[int] = set()
    if slash := SLASH_MATE.fullmatch(identifier):
        named.add(int(slash[1]))
        identifier = identifier[:-2]
    elif sra := SRA_READ_ID.fullmatch(identifier):
        named.add(int(sra[2]))
        identifier = sra[1]
    for position, token in enumerate(rest):
        if slash := SLASH_MATE.fullmatch(token):
            named.add(int(slash[1]))
        elif position == 0 and (casava := CASAVA_MATE.fullmatch(token)):
            named.add(int(casava[1]))
    if len(named) > 1:
        raise ValueError("a read's title names both mates.")
    if named and named != {mate}:
        raise ValueError(
            f"read {mate}'s file has a read whose title marks it mate {3 - mate}: "
            "the two files may be swapped."
        )
    return identifier


def statistics(*paths: Path) -> list[dict[str, int | float]]:
    summaries = [Summary() for _ in paths]
    with ExitStack() as stack:
        sources = []
        for path in paths:
            raw = stack.enter_context(path.open("rb"))
            source = (
                stack.enter_context(gzip.GzipFile(fileobj=raw))
                if raw.peek(2)[:2] == b"\x1f\x8b"
                else raw
            )
            sources.append(read_fastq(source))

        for number, reads in enumerate(zip_longest(*sources), 1):
            if len(reads) == 2:
                first, second = reads
                if first is None or second is None:
                    raise ValueError("the mate files have different read counts.")
                if mate_id(first[0], 1) != mate_id(second[0], 2):
                    raise ValueError(f"the mate identifiers differ at read {number}.")
            for summary, read in zip(summaries, reads, strict=True):
                if read is not None:
                    summary.add(read[1])
    if not summaries[0].reads:
        raise ValueError("the FASTQ contains no reads.")
    return [summary.values() for summary in summaries]


def summarize(run: dict[str, Any]) -> dict[str, Any]:
    read_set = run["inputs"]["reads"][0]
    library = read_set["included"]["library"][0]
    reads = read_set["values"]
    file_ids = [reads["read_1"]]
    if reads.get("read_2"):
        file_ids.append(reads["read_2"])
    files = [run["files"][file_id] for file_id in file_ids]
    if len(files) == 2 and files[0]["sha256"] == files[1]["sha256"]:
        raise ValueError("the two mate files have identical content.")
    summaries = statistics(*(Path(file["path"]) for file in files))
    return {
        "apiVersion": "iki/v1",
        "kind": "RunOutput",
        "creates": [
            {
                "type": "iki/sequencing.FASTQSummary",
                "derived_from": [read_set["id"]],
                "values": {
                    "read_set": read_set["id"],
                    "sample": library["values"]["sample"],
                    "read_set_revision": read_set["revision"],
                    "library_revision": library["revision"],
                    "files": file_ids,
                    "statistics": [
                        {"mate": mate, "file": file["name"], "sha256": file["sha256"], **summary}
                        for mate, (file, summary) in enumerate(
                            zip(files, summaries, strict=True), 1
                        )
                    ],
                },
            }
        ],
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        output = summarize(run)
    except (EOFError, OSError, ValueError, zlib.error) as exc:
        print(f"::error Cannot summarize FASTQ: {exc}", flush=True)
        raise SystemExit(1) from None
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
