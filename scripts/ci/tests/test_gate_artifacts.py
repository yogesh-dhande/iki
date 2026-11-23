"""Exercise the branch's gate with disposable build manifests and artifacts."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check_test_prerequisites.py"
WASM = b"\x00asm\x01\x00\x00\x00"


def write(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def run_check(root: Path, group: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - this repository's gate script
        [sys.executable, str(SCRIPT), group], cwd=root, capture_output=True, text=True, check=False
    )


def test_builds_modules_declared_on_this_branch(tmp_path: Path) -> None:
    for name in ["web/app/wasm/iki_wasm.wasm", "spec/src/iki_spec/iki_wasm.wasm"]:
        write(tmp_path, name, WASM)
    assert run_check(tmp_path, "wasm").returncode == 0
    sdk = tmp_path / "spec/src/iki_spec/iki_wasm.wasm"
    sdk.write_bytes(b"")
    missing_shared = run_check(tmp_path, "wasm")
    assert missing_shared.returncode != 0 and "scripts/build-wasm.sh" in missing_shared.stderr
    sdk.write_bytes(WASM)
    module = "plugins/sample/data/sample.wasm"
    copy = "spec/src/iki_spec/modules/sample.wasm"
    write(
        tmp_path, module + ".sha256", f"{hashlib.sha256(WASM).hexdigest()}  sample.wasm\n".encode()
    )
    missing_builder = run_check(tmp_path, "wasm")
    assert missing_builder.returncode != 0
    assert "scripts/build-sample-wasm.sh" in missing_builder.stderr
    build = write(tmp_path, "scripts/build-sample-wasm.sh", b"#!/bin/sh\nexit 0\n")
    build.chmod(0o755)
    missing_module = run_check(tmp_path, "wasm")
    assert missing_module.returncode != 0
    assert module in missing_module.stderr
    build.write_text(
        f"#!/bin/sh\nmkdir -p spec/src/iki_spec/modules\n"
        f"cp web/app/wasm/iki_wasm.wasm {module}\ncp {module} {copy}\n"
    )
    assert run_check(tmp_path, "wasm").returncode == 0
    build.write_text("#!/bin/sh\nexit 0\n")
    write(tmp_path, copy, WASM + b"wrong build")
    mismatch = run_check(tmp_path, "wasm")
    assert mismatch.returncode != 0
    assert copy in mismatch.stderr
    write(
        tmp_path, module + ".sha256", f"{hashlib.sha256(WASM).hexdigest()}  other.wasm\n".encode()
    )
    wrong_pin = run_check(tmp_path, "wasm")
    assert wrong_pin.returncode != 0 and f"Invalid pin in {module}.sha256" in wrong_pin.stderr


@pytest.mark.parametrize(
    ("missing", "fix"),
    [
        ("app/manifest.json", "make web"),
        ("viewers/assets/bundle.js", "make web"),
        ("app/assets/deferred.wasm", "make web"),
        ("app/assets/nested.wasm", "make web"),
        ("node_modules/axe-core/axe.min.js", "pnpm install --frozen-lockfile"),
        ("web/packages/sample/dist/story.js", "make web"),
        ("web/packages/sample/dist/helper.js", "make web"),
        ("web/packages/sample/dist/fonts/example.woff2", "make web"),
    ],
)
def test_all_web_prerequisites(tmp_path: Path, missing: str, fix: str) -> None:
    static = tmp_path / "server/src/iki_server/web/static"
    for area in ["app", "viewers"]:
        manifest = {"index.ts": {"isEntry": True, "file": "assets/bundle.js"}}
        if area == "app":
            manifest["worker"] = {"file": "assets/deferred.wasm"}
            write(static, "app/assets/deferred.wasm", WASM)
            manifest["worker-entry"] = {"file": "assets/worker.js"}
            write(static, "app/assets/worker.js", b"fetch(`/static/app/assets/nested.wasm`)")
            write(static, "app/assets/nested.wasm", WASM)
        write(static / area, "manifest.json", json.dumps(manifest).encode())
        write(static / area, "assets/bundle.js", b"export {};")
    write(tmp_path, "node_modules/axe-core/axe.min.js", b"axe()")
    package = tmp_path / "web/packages/sample"
    write(package, "stories/sample.html", b"<script type=module></script>")
    exports = {
        "./story": {"default": "./dist/story.js"},
        "./fonts.css": {"default": "./dist/fonts.css"},
    }
    write(package, "package.json", json.dumps({"exports": exports}).encode())
    write(package, "dist/story.js", b'import { shared } from "./helper.js";')
    write(package, "dist/helper.js", b"export const shared = true;")
    write(package, "dist/fonts.css", b'@font-face { src: url("fonts/example.woff2") }')
    write(package, "dist/fonts/example.woff2", b"font")
    assert run_check(tmp_path, "web").returncode == 0
    ((tmp_path if missing.startswith(("node_modules/", "web/")) else static) / missing).unlink()
    failure = run_check(tmp_path, "web")
    assert failure.returncode != 0 and missing in failure.stderr and fix in failure.stderr


@pytest.mark.parametrize("marker", ["sample.sha256", "sample.wasm.sha256"])
def test_builds_packaged_module_and_checks_delivered_bytes(tmp_path: Path, marker: str) -> None:
    for name in ["web/app/wasm/iki_wasm.wasm", "spec/src/iki_spec/iki_wasm.wasm"]:
        write(tmp_path, name, WASM)
    write(tmp_path, "worker/crates/iki-wasm/standalone/sample/Cargo.toml", b"[package]")
    write(tmp_path, "plugins/sample/data/unrelated.sha256", b"lock for a different asset")
    lock = write(
        tmp_path, f"plugins/sample/data/{marker}", hashlib.sha256(WASM).hexdigest().encode()
    )
    builder = "scripts/build-sample-wasm.sh"
    missing_builder = run_check(tmp_path, "wasm")
    assert missing_builder.returncode != 0 and builder in missing_builder.stderr
    script = write(tmp_path, builder, b"#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    package = tmp_path / "spec/src/iki_spec"
    module = package / "sample.wasm"
    missing = run_check(tmp_path, "wasm")
    assert missing.returncode != 0 and "spec/src/iki_spec/sample.wasm" in missing.stderr
    script.write_text("#!/bin/sh\ncp web/app/wasm/iki_wasm.wasm spec/src/iki_spec/sample.wasm\n")
    notice = package / "rust-standard-library.html"
    missing_notice = run_check(tmp_path, "wasm")
    assert missing_notice.returncode != 0 and notice.name in missing_notice.stderr
    notice.write_text("<p>Rust library notice</p>")
    assert run_check(tmp_path, "wasm").returncode == 0
    assert module.read_bytes() == WASM
    lock.write_text("0" * 64)
    mismatch = run_check(tmp_path, "wasm")
    assert mismatch.returncode != 0 and marker in mismatch.stderr
    lock.unlink()
    no_lock = run_check(tmp_path, "wasm")
    assert no_lock.returncode != 0 and "sample.sha256" in no_lock.stderr
