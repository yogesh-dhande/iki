"""Gives each request the agreed rank of the project that asked for it.

The run is given the requests, each with the project that asked (``request.asking_project``:
its ID, slug and name), and the core's whole ranking, every Project served it keeps
(ADR 0232). Only agreed ranks count: a proposed one waits for a coordinator. A project is
matched by its slug, which never changes. A request whose project has no agreed rank has
none, so a project taken out of the ranking loses its rank the next time its requests are
ranked.

It reads the run and writes its results with Iki's action helper, iki_action.py.
"""

import iki_action as iki


def agreed_ranks(ranking: list[dict]) -> dict[str, int]:
    """Each ranked project's slug, and its agreed rank."""
    return {
        row["values"]["project_slug"]: row["values"]["rank"]
        for row in ranking
        if row["values"].get("status") == "agreed"
    }


def rank(run: iki.Run) -> iki.Output:
    ranks = agreed_ranks(run.inputs("ranking"))
    out = run.output()
    for request in run.inputs("requests"):
        asking = (request.get("request") or {}).get("asking_project") or {}
        rank = ranks.get(asking.get("slug", ""))
        if rank != request["values"].get("project_rank"):
            out.update("requests", request, {"project_rank": rank})
        said = f"rank {rank}" if rank is not None else "no agreed rank"
        print(f"{request['hrid']}: {asking.get('name', 'its project')}, {said}")
    return out


if __name__ == "__main__":
    iki.main(rank)
