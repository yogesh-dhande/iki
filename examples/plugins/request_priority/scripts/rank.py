"""Gives each request the agreed rank of the project that asked for it.

The run is given the requests, each with the project that asked (``request.asking_project``:
its ID, slug and name), and the core's whole ranking, every Project served it keeps
(ADR 0232). Only agreed ranks count: a proposed one waits for a coordinator. A project is
matched by its slug, which never changes. A request whose project has no agreed rank has
none, so a project taken out of the ranking loses its rank the next time its requests are
ranked.

Like every action's command, it reads the run from $IKI_INPUT and writes its results to
$IKI_OUTPUT.
"""

import json
import os
from pathlib import Path


def agreed_ranks(ranking: list[dict]) -> dict[str, int]:
    """Each ranked project's slug, and its agreed rank."""
    return {
        row["values"]["project_slug"]: row["values"]["rank"]
        for row in ranking
        if row["values"].get("status") == "agreed"
    }


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    ranks = agreed_ranks(run["inputs"].get("ranking", []))
    updates = []
    for request in run["inputs"]["requests"]:
        asking = (request.get("request") or {}).get("asking_project") or {}
        rank = ranks.get(asking.get("slug", ""))
        if rank != request["values"].get("project_rank"):
            updates.append({"entity": request["id"], "values": {"project_rank": rank}})
        said = f"rank {rank}" if rank is not None else "no agreed rank"
        print(f"{request['hrid']}: {asking.get('name', 'its project')}, {said}")
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "updates": {"requests": updates}}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
