"""Records minimap2's answers for the whole-plasmid tests (see replay.py), in the plugin's
environment, which has minimap2: from the plugin's directory, `python -I
tests/record_minimap2.py`."""

from __future__ import annotations

import gzip
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    spec = importlib.util.spec_from_file_location("replay", HERE / "replay.py")
    assert spec is not None and spec.loader is not None
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    script = replay.module()
    calls: dict[str, list[str]] = {}
    script.minimap2 = replay.recording(calls)
    version = subprocess.run(
        ["minimap2", "--version"],  # noqa: S607 - the environment's
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        for name, (given, out_dir) in replay.scenarios(Path(tmp)).items():
            print(name, flush=True)  # noqa: T201 - a script, saying what it records
            replay.run(script, given, out_dir)
    raw = json.dumps({"version": version, "calls": dict(sorted(calls.items()))}, indent=0)
    # mtime 0, so the same answers give the same file.
    with gzip.GzipFile(replay.REPLAY, "wb", compresslevel=9, mtime=0) as handle:
        handle.write(raw.encode("utf-8"))
    print(f"{len(calls)} calls, minimap2 {version}")  # noqa: T201 - a script


if __name__ == "__main__":
    main()
