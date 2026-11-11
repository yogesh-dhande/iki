"""The DNA plugin's NCBI fetch: `accessions` names nucleotide records to fetch from NCBI.

It asks NCBI's E-utilities (efetch, nuccore) for each record in GenBank format, with its
features, reads them as an imported GenBank file is read, and writes the sequences to
create, each saying which record and version it came from and when it was fetched.

It follows NCBI's rules for automated clients: it gives a tool name and the address the
deployment set in the plugin's `ncbi_email` setting, asks for up to 100 records at a time,
and makes no more than three requests a second. NCBI's answers that ask it to slow down
(429) or that fail on its side (5xx) are retried, with backoff.

All or nothing: an accession NCBI has no record for fails the run with a `::error` line
that names it.
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

from imports import Record, UnreadableError, finish, genbank

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL = "iki-dna"
ACCESSION = re.compile(r"^[A-Z]{1,6}_?[0-9]{1,9}(\.[0-9]+)?$")
MAX_ACCESSIONS = 500
BATCH = 100
#: NCBI allows three requests a second without an API key.
PAUSE = 0.34
ATTEMPTS = 4
MAX_BYTES = 1024**3


def accessions(text: str) -> list[str]:
    """The accessions in `text`, in order, each once, upper-cased."""
    found = list(dict.fromkeys(w.upper() for w in re.split(r"[\s,;]+", text) if w))
    if not found:
        raise UnreadableError("Give at least one accession, such as NM_000546.6.")
    for word in found:
        if not ACCESSION.match(word):
            raise UnreadableError(
                f"{word} isn't a nucleotide accession, such as NM_000546.6 or L09137."
            )
    if len(found) > MAX_ACCESSIONS:
        raise UnreadableError(
            f"That's {len(found):,} accessions, more than the {MAX_ACCESSIONS} one run "
            "fetches: split them."
        )
    return found


def efetch(ids: list[str], email: str) -> str:
    """GenBank text for `ids`, from NCBI."""
    body = urllib.parse.urlencode(
        {
            "db": "nuccore",
            "id": ",".join(ids),
            "rettype": "gbwithparts",
            "retmode": "text",
            "tool": TOOL,
            "email": email,
        }
    ).encode()
    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(EFETCH, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                data = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            error.close()
            if attempt < ATTEMPTS and (error.code == 429 or error.code >= 500):
                time.sleep(2**attempt)
                continue
            if error.code == 400:
                # NCBI answers 400 when none of the IDs is a record it holds.
                return ""
            raise UnreadableError(f"NCBI answered {error.code} {error.reason}.") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < ATTEMPTS:
                time.sleep(2**attempt)
                continue
            reason = getattr(error, "reason", error)
            raise UnreadableError(f"Couldn't reach NCBI: {reason}.") from None
        if len(data) > MAX_BYTES:
            raise UnreadableError("NCBI's answer is over 1 GiB: fetch fewer records at once.")
        return data.decode("utf-8", errors="replace")
    raise AssertionError("unreachable")


def matches(asked: str, record: Record) -> bool:
    versioned = str(record.values.get("accession") or "")
    return asked in (versioned, versioned.split(".")[0])


def fetch(asked: list[str], email: str) -> list[Record]:
    """The records for `asked`, in its order."""
    records: list[Record] = []
    for start in range(0, len(asked), BATCH):
        if start:
            time.sleep(PAUSE)
        batch = asked[start : start + BATCH]
        text = efetch(batch, email)
        got = genbank(text) if text.strip() else []
        print(f"::progress {min(start + BATCH, len(asked)) / len(asked):.2f} Fetched", flush=True)
        for accession in batch:
            found = next((r for r in got if matches(accession, r)), None)
            if found is None:
                raise UnreadableError(f"NCBI has no nucleotide record {accession}.")
            found.label = str(found.values.get("accession") or accession)
            records.append(found)
    return records


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    email = (run.get("settings") or {}).get("ncbi_email")
    try:
        if not email:
            raise UnreadableError(
                "The dna plugin's ncbi_email setting isn't set: NCBI asks automated "
                "clients for an address. Ask your Iki administrator to set it."
            )
        records = fetch(accessions(run["params"]["accessions"]), email)
    except UnreadableError as why:
        print(f"::error {why}", flush=True)
        sys.exit(1)
    retrieved = datetime.now(UTC).isoformat(timespec="seconds")

    def origin(record: Record) -> dict[str, Any]:
        versioned = str(record.values.get("accession") or record.label)
        return {
            "source": "NCBI Nucleotide",
            "id": versioned.split(".")[0],
            "version": versioned,
            "url": f"https://www.ncbi.nlm.nih.gov/nuccore/{versioned}",
            "retrieved_at": retrieved,
            "sha256": hashlib.sha256(record.text.encode("utf-8")).hexdigest(),
        }

    finish(records, origin)


if __name__ == "__main__":
    main()
