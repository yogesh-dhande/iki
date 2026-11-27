"""Assign the configured storage code to each sample with an empty storage location."""

import iki_action as iki


def assign(run: iki.Run) -> iki.Output:
    out = run.output()
    for sample in run.inputs("sample"):
        if not sample.values.get("storage"):
            out.update("sample", sample, {"storage": run.settings["default_storage"]})
    return out


if __name__ == "__main__":
    iki.main(assign)
