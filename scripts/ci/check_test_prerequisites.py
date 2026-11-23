"""Build the modules declared by this checkout and check the assets its tests load."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

WASM_HEADER = b"\x00asm\x01\x00\x00\x00"


def populated(path: Path, fix: str) -> None:
    if not path.is_file() or not path.stat().st_size:
        raise SystemExit(f"{path} is missing or empty. Run {fix}.")


def wasm_copies(source: Path, installed: Path, builder: Path) -> None:
    for path in (source, installed):
        populated(path, str(builder))
    binary = source.read_bytes()
    if not binary.startswith(WASM_HEADER) or installed.read_bytes() != binary:
        raise SystemExit(f"Invalid or mismatched modules {source} and {installed}. Run {builder}.")


def wasm() -> None:
    package = Path("spec/src/iki_spec")
    wasm_copies(
        Path("web/app/wasm/iki_wasm.wasm"), package / "iki_wasm.wasm", Path("scripts/build-wasm.sh")
    )
    # A standalone crate or builder declares a module even if its lock was removed.
    # Other data files can have checksums too.
    locks = sorted(Path("plugins").glob("*/data/*.sha256"))
    modules = {
        lock.name.removesuffix(".wasm.sha256")
        for lock in locks
        if lock.name.endswith(".wasm.sha256")
    }
    modules.update(
        p.parent.name for p in Path("worker/crates/iki-wasm/standalone").glob("*/Cargo.toml")
    )
    modules.update(
        p.name.removeprefix("build-").removesuffix("-wasm.sh")
        for p in Path("scripts").glob("build-*-wasm.sh")
    )
    for name in sorted(modules):
        builder = Path("scripts") / f"build-{name}-wasm.sh"
        matching = [
            lock for lock in locks if lock.name in (f"{name}.wasm.sha256", f"{name}.sha256")
        ]
        if len(matching) != 1:
            raise SystemExit(
                f"Module {name} needs exactly one {name}.sha256 or {name}.wasm.sha256 "
                f"under plugins/*/data. Found {matching}."
            )
        lock = matching[0]
        if not builder.is_file():
            raise SystemExit(f"{lock} declares {name}. Restore its builder {builder}.")
        try:
            subprocess.run([str(builder)], check=True)  # noqa: S603 - repository build script
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"Failed to build {name}. Run {builder}: {exc}") from exc
        if lock.name.endswith(".wasm.sha256"):
            module = lock.with_suffix("")
            wasm_copies(module, package / "modules" / module.name, builder)
        else:
            # Package-only builders install their canonical bytes alongside iki_wasm.
            module = package / f"{name}.wasm"
            populated(module, str(builder))
            binary = module.read_bytes()
            if (
                not binary.startswith(WASM_HEADER)
                or hashlib.sha256(binary).hexdigest() != lock.read_text().strip()
            ):
                raise SystemExit(f"{module} does not match {lock}. Run {builder} --check.")
            populated(package / "rust-standard-library.html", str(builder))


def web() -> None:
    populated(Path("node_modules/axe-core/axe.min.js"), "pnpm install --frozen-lockfile")
    for name in ("app", "viewers"):
        out = Path("server/src/iki_server/web/static") / name
        path = out / "manifest.json"
        populated(path, "make web")
        # Vite lists the worker script in the manifest. WASM emitted by a worker's
        # own build is referenced only by its URL in that script.
        wasm_url = re.compile(
            rb"""["'`]/static/""" + name.encode() + rb"""/(assets/[\w.-]+\.wasm)["'`]"""
        )
        try:
            manifest = json.loads(path.read_text())
            if not manifest or not any(entry.get("isEntry") for entry in manifest.values()):
                raise ValueError("no entry points")
            for entry in manifest.values():
                for key in [*entry.get("imports", []), *entry.get("dynamicImports", [])]:
                    if key not in manifest:
                        raise ValueError(f"unlisted import {key}")
                for file in [entry["file"], *entry.get("css", []), *entry.get("assets", [])]:
                    asset = out / file
                    populated(asset, "make web")
                    if asset.suffix == ".js":
                        for target in wasm_url.findall(asset.read_bytes()):
                            populated(out / target.decode(), "make web")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise SystemExit(f"Invalid {path} ({exc}). Run make web.") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("wasm", "web"))
    if parser.parse_args().group == "wasm":
        wasm()
    else:
        web()
