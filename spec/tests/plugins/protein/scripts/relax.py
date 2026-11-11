"""Write a relaxed copy of a structure (an example: it marks the file rather than
minimizing anything).

It reads its input file from where iki-worker put it (input.json's `files`), and writes
the new one to $IKI_OUT_DIR, naming it in output.json with `{"$file": …}`, which
iki-worker uploads and replaces with the file's ID.
"""

import json
import os
from pathlib import Path


def main() -> None:
    run = json.loads(Path(os.environ["IKI_INPUT"]).read_text(encoding="utf-8"))
    (structure,) = run["inputs"]["structure"]
    source = run["files"][structure["values"]["structure"]]
    original = Path(source["path"]).read_bytes()
    print(f"Read {source['name']}: {len(original)} bytes")
    relaxed = b"REMARK   1 RELAXED BY IKI EXAMPLE\n" + original
    (Path(os.environ["IKI_OUT_DIR"]) / "relaxed.pdb").write_bytes(relaxed)
    made = {
        "type": "protein.ProteinStructure",
        "values": {
            "structure": {"$file": "relaxed.pdb", "media_type": "chemical/x-pdb"},
            "method": "predicted",
            "chains": structure["values"]["chains"],
        },
    }
    output = {"apiVersion": "iki/v1", "kind": "RunOutput", "creates": [made]}
    Path(os.environ["IKI_OUTPUT"]).write_text(json.dumps(output), encoding="utf-8")


if __name__ == "__main__":
    main()
