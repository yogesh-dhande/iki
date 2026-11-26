"""Build the modules declared by this checkout and check the assets its tests load."""

import argparse
import hashlib
import json
import platform
import re
import subprocess
from pathlib import Path

WASM_HEADER = b"\x00asm\x01\x00\x00\x00"


def populated(path: Path, fix: str) -> None:
    if not path.is_file() or not path.stat().st_size:
        raise SystemExit(f"{path} is missing or empty. Run {fix}.")


def wasm_copies(source: Path, installed: Path, builder: Path) -> bytes:
    for path in (source, installed):
        populated(path, str(builder))
    binary = source.read_bytes()
    if not binary.startswith(WASM_HEADER) or installed.read_bytes() != binary:
        raise SystemExit(f"Invalid or mismatched modules {source} and {installed}. Run {builder}.")
    return binary


def linux_x86_64() -> bool:
    return platform.system() == "Linux" and platform.machine().lower() in ("x86_64", "amd64")


def oligo_module(package: Path, builder: Path, pin: Path) -> None:
    """build-wasm.sh already installed both copies using this output-taking builder."""
    filename = "oligo-measurement.wasm"
    factory = Path("scripts/build-wasm.sh")
    digest = pin.read_text().rstrip("\n") if pin.is_file() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit(f"Restore the release SHA-256 pin in {pin}.")
    if not builder.is_file():
        raise SystemExit(f"Restore {builder}, which builds {filename}.")
    module = package / filename
    binary = wasm_copies(module, pin.parent / filename, factory)
    populated(package / "rust-standard-library.html", str(factory))
    if linux_x86_64():
        if hashlib.sha256(binary).hexdigest() != digest:
            raise SystemExit(f"{module} does not match {pin}. Run {factory}.")
    else:
        # Release pins come from Linux x86-64. On another host, compare to the
        # pinned Rust toolchain's fresh native build instead.
        try:
            subprocess.run([str(builder), "--check", str(module)], check=True)  # noqa: S603
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"{module} failed {builder} --check. Run {factory}.") from exc


def wasm() -> None:
    package = Path("spec/src/iki_spec")
    for consumer in ("web/app/wasm", "web/packages/science/wasm"):
        wasm_copies(
            package / "iki_wasm.wasm",
            Path(consumer) / "iki_wasm.wasm",
            Path("scripts/build-wasm.sh"),
        )
    # The file previews' readers, which the web app's tests load (ADR 0226).
    previews = Path("web/app/wasm/iki_previews.wasm")
    populated(previews, "scripts/build-wasm.sh")
    if not previews.read_bytes().startswith(WASM_HEADER):
        raise SystemExit(f"{previews} isn't WebAssembly. Run scripts/build-wasm.sh.")
    # This standalone is an individual Rust source. Its installed filename and pin
    # differ from its builder's stem, and the shared factory supplies its output path.
    oligo_builder = Path("scripts/build-oligo-wasm.sh")
    oligo_pin = Path("plugins/dna/data/oligo-measurement.sha256")
    oligo_source = Path("worker/crates/iki-wasm/standalone/oligo.rs")
    if any(path.exists() for path in (oligo_builder, oligo_pin, oligo_source)):
        oligo_module(package, oligo_builder, oligo_pin)
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
        if p != oligo_builder
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
        # The pin's contents describe the distribution. Both forms use .wasm.sha256.
        # A filename in a sha256sum record names the plugin's mirrored module.
        pin = lock.read_text().split()
        filename = f"{name}.wasm"
        if (
            len(pin) not in (1, 2)
            or not re.fullmatch(r"[0-9a-f]{64}", pin[0])
            or (len(pin) == 2 and pin[1].removeprefix("*") != filename)
        ):
            raise SystemExit(f"Invalid pin in {lock}. Expected a SHA-256 digest for {filename}.")
        try:
            subprocess.run([str(builder)], check=True)  # noqa: S603 - repository build script
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemExit(f"Failed to build {name}. Run {builder}: {exc}") from exc
        if len(pin) == 2:
            module = lock.parent / filename
            binary = wasm_copies(module, package / "modules" / filename, builder)
            if linux_x86_64() and hashlib.sha256(binary).hexdigest() != pin[0]:
                raise SystemExit(f"{module} does not match {lock}. Run {builder}.")
        else:
            # Package-only builders install their canonical bytes alongside iki_wasm.
            module = package / filename
            populated(module, str(builder))
            binary = module.read_bytes()
            if not binary.startswith(WASM_HEADER) or hashlib.sha256(binary).hexdigest() != pin[0]:
                raise SystemExit(f"{module} does not match {lock}. Run {builder} --check.")
            populated(package / "rust-standard-library.html", str(builder))


def story_assets(packages: Path) -> None:
    """Check the published entries, the gallery's separate bundles and their local assets."""
    pending: list[Path] = []
    for stories in packages.glob("*/stories"):
        package = stories.parent
        manifest = json.loads((package / "package.json").read_text())
        pending.extend(package / entry["default"] for entry in manifest["exports"].values())
        pending.extend(stories.glob("*.html"))
    checked: set[Path] = set()
    while pending:
        asset = pending.pop().resolve()
        if asset in checked:
            continue
        checked.add(asset)
        populated(asset, "make web")
        if asset.suffix in {".js", ".html"}:
            source = asset.read_text()
            paths = re.findall(
                r"""\b(?:from\s*|import\s*(?:\(\s*)?)["'](\.{1,2}/[^"']+)["']""", source
            )
            # The package's parser loads WASM this way. The gallery bundler also
            # emits these URLs for CSS, relative to the entry or a hashed chunk.
            paths.extend(
                re.findall(
                    r"""\bnew URL\(\s*["'`]((?!/)[\w./-]+\.\w+)(?:\?[^"'`]*)?["'`]"""
                    r"""\s*,\s*import\.meta\.url\s*\)""",
                    source,
                )
            )
            if asset.suffix == ".html":
                paths.extend(
                    re.findall(
                        r"""(?:<(?:link|script)\b[^>]*?\b(?:href|src)=|\bfetch\(\s*)"""
                        r"""["'](\.{1,2}/[\w./-]+)["']""",
                        source,
                    )
                )
        elif asset.suffix == ".css":
            paths = re.findall(r"""url\(\s*["']?([\w./-]+)["']?\s*\)""", asset.read_text())
        else:
            continue
        pending.extend(asset.parent / path for path in paths)


def app_assets(static: Path) -> None:
    for name in ("app", "viewers"):
        out = static / name
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


def docs() -> None:
    """The docs site, built and indexed, with each page's policy (make docs)."""
    site = Path("docs/site/dist")
    for path in (
        "index.html",
        "pagefind/pagefind.js",
        "use-cases/verify-a-plasmid-prep/index.html",
    ):
        populated(site / path, "make docs")
    if 'http-equiv="Content-Security-Policy"' not in (site / "index.html").read_text():
        raise SystemExit(f"{site} has no Content Security Policy. Run make docs.")


def web() -> None:
    populated(Path("node_modules/axe-core/axe.min.js"), "pnpm install --frozen-lockfile")
    app_assets(Path("server/src/iki_server/web/static"))
    story_assets(Path("web/packages"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("group", choices=("wasm", "web", "docs"))
    group = parser.parse_args().group
    if group == "wasm":
        wasm()
    elif group == "docs":
        docs()
    else:
        web()
