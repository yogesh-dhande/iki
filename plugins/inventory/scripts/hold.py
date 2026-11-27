"""Carry a container's withdrawal reason to its derived stock."""

import iki_action as iki


def hold(run: iki.Run) -> iki.Output:
    source = run.input("source")
    out = run.output()
    if not source.values.get("on_hold"):
        return out
    why = run.params.get("reason") or source.values["hold_reason"]
    reason = f"Withdrawn stock {source.hrid}: {why.strip()}"
    changes = [tube for tube in run.inputs("tubes") if not tube.values.get("on_hold")]
    if not changes:
        return out
    # Check the source revision in the same transaction as the descendant holds.
    # Writing the existing reason changes no value if the source is still current.
    out.update(
        "source", source, {"hold_reason": source.values["hold_reason"]}, require_unchanged=True
    )
    for tube in changes:
        out.update("tubes", tube, {"on_hold": True, "hold_reason": reason}, require_unchanged=True)
    return out


if __name__ == "__main__":
    iki.main(hold)
