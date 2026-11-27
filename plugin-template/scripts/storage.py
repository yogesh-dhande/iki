"""Record a sample's storage location the first time it is assigned."""

import iki_action as iki


def assign(run: iki.Run) -> iki.Output:
    sample = run.input("sample")
    out = run.output()
    if not sample.values.get("storage"):
        out.update("sample", sample, {"storage": run.params["location"]})
    return out


if __name__ == "__main__":
    iki.main(assign)
