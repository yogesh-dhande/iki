"""Check the shipped agent skills against this checkout and the real Iki CLI."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest
import yaml

from iki_cli.main import build_parser
from iki_cli.workers import bundle

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "plugin-template"
SKILLS = TEMPLATE / ".agents/skills"
MAIN_LINK = re.compile(r"https://github\.com/yogesh-dhande/iki/(?:blob|tree)/main/([^\s)#]+)")


@pytest.mark.parametrize("operation", ["write", "test", "upgrade"])
def test_skill_commands_and_paths_exist(operation: str) -> None:
    name = f"iki-{operation}-plugin"
    skill = SKILLS / name
    for root in (ROOT / ".agents/skills", ROOT / ".claude/skills"):
        assert (root / name).resolve() == skill

    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    start, frontmatter, body = text.split("---\n", 2)
    assert start == ""
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == name
    assert metadata["description"].strip()
    entry = TEMPLATE / ".claude/skills" / name / "SKILL.md"
    _, entry_frontmatter, entry_body = entry.read_text(encoding="utf-8").split("---\n", 2)
    assert yaml.safe_load(entry_frontmatter) == metadata
    assert f"](../../../.agents/skills/{name}/SKILL.md)" in entry_body

    for path in MAIN_LINK.findall(body):
        assert (ROOT / path).exists(), f"{name} links to a missing path on main: {path}"

    parser = build_parser()
    for example in re.findall(r"```sh\n(.*?)\n```", body, flags=re.S):
        for line in example.replace("\\\n", "").splitlines():
            words = shlex.split(line, comments=True)
            if not words:
                continue
            command, *args = words
            assert command in {"iki", "make", "sh", "uv"}, line
            if command == "iki":
                parser.parse_args(args)
            elif command == "make":
                assert args and all(re.fullmatch(r"[a-z][a-z0-9-]*", arg) for arg in args)
                targets = subprocess.run(  # noqa: S603 - dry run of documented targets
                    ["make", "-n", *args],  # noqa: S607 - make from the PATH
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert targets.returncode == 0, targets.stderr
            elif command == "uv":
                assert args[:2] == ["run", "pytest"], line
            elif command == "sh":
                assert args and args[0].startswith("$SKILL_DIR/scripts/"), line
            for arg in args:
                if arg.startswith("$SKILL_DIR/"):
                    assert (skill / arg.removeprefix("$SKILL_DIR/")).is_file(), line
                elif "/" in arg and not arg.startswith(("$", "iki/")):
                    assert (ROOT / arg.split("::", 1)[0]).exists(), line


@pytest.fixture
def template(tmp_path: Path) -> Path:
    return shutil.copytree(TEMPLATE, tmp_path / "my lab plugin", symlinks=True)


def test_the_template_copy_includes_skills_only_for_authors(template: Path, tmp_path: Path) -> None:
    archive = tmp_path / "plugin.tar.gz"
    bundle("acme/example", template, archive)
    with tarfile.open(archive, "r:gz") as packaged:
        members = set(packaged.getnames())
    for guide in SKILLS.glob("*/SKILL.md"):
        skill = guide.parent
        for file in (
            guide,
            TEMPLATE / ".claude/skills" / skill.name / "SKILL.md",
            *(skill / "scripts").glob("*.sh"),
        ):
            relative = file.relative_to(TEMPLATE)
            assert (template / relative).read_bytes() == file.read_bytes()
            assert relative.as_posix() not in members


def run_skill(
    template: Path, script: str, *args: str | Path, **settings: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a shipped script running the local Iki CLI
        ["sh", str(template / ".agents/skills" / script), *map(str, args)],  # noqa: S607
        cwd=template,
        env={
            **os.environ,
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]),
            **settings,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_write_skill_generates_the_initial_migration(template: Path) -> None:
    shutil.rmtree(template / "migrations")
    checked = run_skill(template, "iki-write-plugin/scripts/validate-and-migrate.sh", ".")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert (template / "migrations/0001_initial.py").is_file()


@pytest.mark.parametrize("blocking", [False, True])
def test_upgrade_skill_sends_the_built_candidate(
    template: Path, tmp_path: Path, blocking: bool
) -> None:
    received: list[tuple[str, str | None, dict[str, dict[str, str]]]] = []

    class Preflight(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers["Authorization"], body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"blocking": blocking}).encode())

    with HTTPServer(("127.0.0.1", 0), Preflight) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            checked = run_skill(
                template,
                "iki-upgrade-plugin/scripts/build-and-preflight.sh",
                "acme/example",
                ".",
                IKI_URL=f"http://127.0.0.1:{server.server_port}",
                IKI_TOKEN="fixture-token",
                TMPDIR=str(tmp_path),
            )
        finally:
            server.shutdown()
            thread.join()
    assert checked.returncode == int(blocking), checked.stdout + checked.stderr
    assert json.loads(checked.stdout) == {"blocking": blocking}
    [(path, auth, manifest)] = received
    assert path == "/api/v1/plugins/acme/example/preflight"
    assert auth == "Bearer fixture-token"
    assert manifest["plugin"]["name"] == "acme/example"
    assert not list(tmp_path.glob("iki-skill-preflight.*"))
