"""Read-only FASTQ quality statistics for an Iki sequencing read set."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any

import iki_action as iki

from iki_runtime import fastq, text
from iki_runtime.fastq import MAX_READ_LENGTH, PHRED_OFFSET

# Cock et al. (2010), Table 1, Sanger FASTQ, https://doi.org/10.1093/nar/gkp1137.
BELOW_Q30 = bytes(range(PHRED_OFFSET, PHRED_OFFSET + 30))


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


def mate_id(title: bytes, mate: int) -> bytes:
    identifier, *rest = title.split(maxsplit=1)
    suffix = b"/1" if mate == 1 else b"/2"
    if identifier.endswith((b"/1", b"/2")) and not identifier.endswith(suffix):
        raise ValueError("a read identifier has the wrong mate suffix.")
    if rest and rest[0].startswith((b"1:", b"2:")) and not rest[0].startswith(suffix[1:] + b":"):
        raise ValueError("an Illumina read identifier has the wrong mate number.")
    return identifier.removesuffix(suffix)


def statistics(*paths: Path) -> list[dict[str, int | float]]:
    summaries = [Summary() for _ in paths]
    with ExitStack() as stack:
        sources = [fastq.reads(stack.enter_context(text.open_bytes(path))) for path in paths]

        for number, reads in enumerate(zip_longest(*sources), 1):
            if len(reads) == 2:
                first, second = reads
                if first is None or second is None:
                    raise ValueError("the mate files have different read counts.")
                if mate_id(first.title, 1) != mate_id(second.title, 2):
                    raise ValueError(f"the mate identifiers differ at read {number}.")
            for summary, read in zip(summaries, reads, strict=True):
                if read is not None:
                    summary.add(read.quality)
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
                    "review_flow": run.get("settings", {}).get("review_flow", "research"),
                },
            }
        ],
    }


def summary(run: iki.Run) -> dict[str, Any]:
    try:
        return summarize(run)
    except (ValueError, *text.UNREADABLE) as exc:
        raise iki.Refused(f"Cannot summarize FASTQ: {exc}") from None


if __name__ == "__main__":
    iki.main(summary)
