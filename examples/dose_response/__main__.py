"""Load the published Tox21 experiment in its own training projects."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import iki

from .tutorial import FLOWS, prepare, state_path


class SdkApi:
    def __init__(self, client: iki.Client) -> None:
        self.client = client

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:  # noqa: ANN401 - JSON returned by the public API
        parsed = urlsplit(path)
        return self.client.request(
            method, parsed.path, params=dict(parse_qsl(parsed.query)), json=json, headers=headers
        )

    def upload(self, project: str, path: Path) -> str:
        return self.client.files.upload(path, project=project, media_type="text/csv")["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--team", type=UUID, required=True, help="Team in which to create the training projects."
    )
    parser.add_argument("--flow", choices=FLOWS, required=True)
    parser.add_argument("--analyst", type=UUID, required=True, help="Analyst's user ID.")
    parser.add_argument("--reviewer", type=UUID, required=True, help="Reviewer's user ID.")
    parser.add_argument("--qa", type=UUID, help="Independent QA signer's user ID for regulated QC.")
    parser.add_argument("--state", type=Path, help="Local receipt for resuming this exercise.")
    args = parser.parse_args()
    with iki.Client() as client:
        receipt = args.state or state_path(client.url, str(args.team), args.flow)
        ids = prepare(
            SdkApi(client),
            team=str(args.team),
            flow=args.flow,
            analyst=str(args.analyst),
            reviewer=str(args.reviewer),
            qa=str(args.qa) if args.qa else None,
            state=receipt,
        )
    sys.stdout.write(json.dumps(ids, indent=2) + "\n")


if __name__ == "__main__":
    main()
