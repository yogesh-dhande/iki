"""The protein plugin's UniProt fetch: `accessions` names UniProtKB entries to fetch.

It asks UniProt's REST API for the entries in FASTA, up to 100 at a time, reads them
as an imported FASTA file is read, and writes the proteins to create, each saying which
entry, sequence version and UniProt release it came from, and when it was fetched.
Answers that ask it to slow down (429) or that fail on UniProt's side (5xx) are retried,
with backoff.

All or nothing: an accession UniProt has no entry for (or one it has merged into
another, or made obsolete) fails the run with a `::error` line that names it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from imports import ACCESSION, Record, UnreadableError, fasta, finish

ACCESSIONS = "https://rest.uniprot.org/uniprotkb/accessions"
MAX_ACCESSIONS = 1000
BATCH = 100
#: Between batches, to be gentle with a free service.
PAUSE = 0.2
ATTEMPTS = 4
MAX_BYTES = 256 * 1024**2


def accessions(text: str) -> list[str]:
    """The accessions in `text`, in order, each once, upper-cased."""
    found = list(dict.fromkeys(w.upper() for w in re.split(r"[\s,;]+", text) if w))
    if not found:
        raise UnreadableError("Give at least one accession, such as P69905.")
    for word in found:
        if not ACCESSION.match(word):
            raise UnreadableError(
                f"{word} isn't a UniProtKB accession, such as P69905 or P69905-2."
            )
    if len(found) > MAX_ACCESSIONS:
        raise UnreadableError(
            f"That's {len(found):,} accessions, more than the {MAX_ACCESSIONS:,} one run "
            "fetches: split them."
        )
    return found


def get(ids: list[str]) -> tuple[str, str]:
    """UniProt's FASTA for `ids`, and the release it's from."""
    query = urllib.parse.urlencode({"accessions": ",".join(ids), "format": "fasta"})
    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(  # noqa: S310 - a fixed https URL
            f"{ACCESSIONS}?{query}", headers={"User-Agent": "iki-protein"}
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                data = response.read(MAX_BYTES + 1)
                release = response.headers.get("X-UniProt-Release", "")
        except urllib.error.HTTPError as error:
            error.close()
            if attempt < ATTEMPTS and (error.code == 429 or error.code >= 500):
                time.sleep(2**attempt)
                continue
            raise UnreadableError(f"UniProt answered {error.code} {error.reason}.") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < ATTEMPTS:
                time.sleep(2**attempt)
                continue
            reason = getattr(error, "reason", error)
            raise UnreadableError(f"Couldn't reach UniProt: {reason}.") from None
        if len(data) > MAX_BYTES:
            raise UnreadableError("UniProt's answer is over 256 MiB: fetch fewer at once.")
        return data.decode("utf-8", errors="replace"), release
    raise AssertionError("unreachable")


def fetch(asked: list[str]) -> list[tuple[Record, str]]:
    """The entries for `asked`, in its order, each with the release it's from."""
    records: list[tuple[Record, str]] = []
    for start in range(0, len(asked), BATCH):
        if start:
            time.sleep(PAUSE)
        batch = asked[start : start + BATCH]
        text, release = get(batch)
        got = {r.values.get("uniprot_accession"): r for r in fasta(text.splitlines(keepends=True))}
        print(f"::progress {min(start + BATCH, len(asked)) / len(asked):.2f} Fetched", flush=True)
        for accession in batch:
            found = got.get(accession)
            if found is None:
                raise UnreadableError(
                    f"UniProt has no entry {accession}: it may have been merged into "
                    "another or made obsolete. Search for it at https://www.uniprot.org."
                )
            found.label = accession
            records.append((found, release))
    return records


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    try:
        fetched = fetch(accessions(run["params"]["accessions"]))
    except UnreadableError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    retrieved = datetime.now(UTC).isoformat(timespec="seconds")
    releases = {record.label: release for record, release in fetched}

    def origin(record: Record) -> dict[str, Any]:
        accession = record.label
        version = ", ".join(
            part
            for part in (
                f"release {releases[accession]}" if releases[accession] else "",
                f"sequence version {record.version}" if record.version else "",
            )
            if part
        )
        found = {
            "source": "UniProtKB",
            "id": accession,
            "url": f"https://www.uniprot.org/uniprotkb/{accession}/entry",
            "retrieved_at": retrieved,
            "sha256": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
        }
        if version:
            found["version"] = version
        return found

    finish([record for record, _ in fetched], origin)


if __name__ == "__main__":
    main()
