#!/usr/bin/env python3
"""Check server AOT code against the checkout without loading native code."""

import hashlib
import json
import re
import struct
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"amd64": ("x86_64", 62), "arm64": ("aarch64", 183)}


def regular(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular file at {path}")
    return path


def pin(name: str) -> str:
    value = regular(ROOT / "server" / f"{name}.sha256").read_text().strip()
    if not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError(f"Invalid SHA-256 pin for {name}")
    return value


def verify(artifacts: Path) -> None:
    with (ROOT / "uv.lock").open("rb") as handle:
        [version] = [
            p["version"] for p in tomllib.load(handle)["package"] if p["name"] == "wasmtime"
        ]
    wasm_hash = pin("iki_wasm.wasm")
    for arch, (cpu, machine) in TARGETS.items():
        directory = artifacts / arch
        if directory.is_symlink():
            raise ValueError(f"Expected a directory at {directory}, found a symbolic link")
        code = regular(directory / "iki_wasm.cwasm")
        expected = pin(f"iki_wasm.{arch}.cwasm")
        with code.open("rb") as handle:
            header = handle.read(64)
            handle.seek(0)
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != expected:
            raise ValueError(f"{code} SHA-256 mismatch. Expected {expected}, actual {digest}")
        # ELF64, little endian, ELF v1, Wasmtime's OSABI (200). Version 50
        # serializes a core module as ET_REL with e_flags = EF_WASMTIME_MODULE (1).
        if (
            len(header) != 64
            or header[:8] != b"\x7fELF\x02\x01\x01\xc8"
            or struct.unpack_from("<HHI", header, 16) != (1, machine, 1)
            or struct.unpack_from("<I", header, 48)[0] != 1
        ):
            raise ValueError(f"{code} is not a Wasmtime core module for {cpu}")
        manifest = json.loads(regular(directory / "iki_wasm.json").read_bytes())
        metadata = {
            "format": 1,
            "consume_fuel": True,
            "wasm_sha256": wasm_hash,
            "wasmtime_version": version,
            "target": f"{cpu}-unknown-linux-gnu",
            "cwasm_sha256": expected,
        }
        if (
            manifest != metadata
            or type(manifest["format"]) is not int
            or manifest["consume_fuel"] is not True
        ):
            raise ValueError(
                f"{directory}/iki_wasm.json does not match the locked module and target"
            )
        sys.stdout.write(f"{arch} precompiled module SHA-256 verified: {expected}\n")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: verify-server-aot.py <artifact-directory>")
    try:
        verify(Path(sys.argv[1]))
    except (OSError, ValueError) as error:
        sys.exit(str(error))
