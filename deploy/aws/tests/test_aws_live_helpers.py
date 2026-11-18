"""The live AWS test's own helpers, and teardown.sh's start, checked offline: they need no
account, so they run everywhere, CI included, while test_aws_live.py's checks don't."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import test_aws_live as live
from aws_inventory import Settings

SETTINGS = Settings(
    role_arn="arn:aws:iam::000000000000:role/r",
    token_command="true",
    name="iki-test-x",
    region="us-east-1",
    dr_region="us-west-2",
    zones=("us-east-1a", "us-east-1b"),
    system_type="m7i-flex.large",
    runs_type="c7i-flex.large",
    database_class="db.t4g.micro",
    cap_hours=2,
    workdir=Path("/nonexistent"),
)


class FakeCodeBuild:
    """CodeBuild with one build still running, which stops once asked to."""

    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.stopped: list[str] = []

    def account(self) -> str:
        return "000000000000"

    def run(self, *argv: str, check: bool = True) -> Any:
        if argv[1:3] == ("codebuild", "list-builds-for-project"):
            assert argv[argv.index("--project-name") + 1] == "iki-test-x-images"
            return SimpleNamespace(returncode=0, stdout=json.dumps({"ids": self.ids}), stderr="")
        assert argv[1:3] == ("codebuild", "stop-build"), argv
        self.stopped.append(argv[argv.index("--id") + 1])
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    def aws_json(self, *args: str, region: str | None = None) -> Any:
        assert args[:2] == ("codebuild", "batch-get-builds")
        running = "iki-test-x-images:running" not in self.stopped
        return {
            "builds": [
                {
                    "id": "iki-test-x-images:done",
                    "buildStatus": "SUCCEEDED",
                    "startTime": "2026-11-18T03:26:08.603000+00:00",
                    "endTime": "2026-11-18T03:28:49.538000+00:00",
                },
                {
                    "id": "iki-test-x-images:running",
                    "buildStatus": "IN_PROGRESS" if running else "STOPPED",
                    "startTime": "2026-11-18T03:30:00+00:00",
                    **({} if running else {"endTime": "2026-11-18T03:40:30+00:00"}),
                },
            ]
        }


def test_a_build_left_running_is_stopped_and_every_build_is_timed() -> None:
    """A stand-up that fails before the images are done leaves CodeBuild billing: tearing
    down stops it, and the cost report gets each build's minutes, not only a finished one's."""
    codebuild = FakeCodeBuild(["iki-test-x-images:done", "iki-test-x-images:running"])
    timed = live.stop_and_time_builds(codebuild, SETTINGS, within=1)  # type: ignore[arg-type]
    assert codebuild.stopped == ["iki-test-x-images:running"]
    assert timed == [
        {"id": "iki-test-x-images:done", "seconds": 161},
        {"id": "iki-test-x-images:running", "seconds": 630},
    ]


def test_no_builds_nothing_to_time() -> None:
    assert live.stop_and_time_builds(FakeCodeBuild([]), SETTINGS) == []  # type: ignore[arg-type]


def test_a_url_is_signed_for_the_bucket_on_s3s_names_for_its_region() -> None:
    """Live run iki-test-11180446's URLs named S3's global host, as the SDK signs in
    us-east-1; anywhere else only the region's own."""
    assert live.s3_hosts("b", "us-east-1") == {"b.s3.us-east-1.amazonaws.com", "b.s3.amazonaws.com"}
    assert live.s3_hosts("b", "eu-west-1") == {"b.s3.eu-west-1.amazonaws.com"}
    assert "other.s3.amazonaws.com" not in live.s3_hosts("b", "us-east-1")


TEARDOWN = Path(__file__).resolve().parents[1] / "scripts" / "teardown.sh"

FAKE_TOFU = """#!/usr/bin/env bash
# deploy/aws/production's variables through `tofu console`, which fails while a lock is held.
[[ $2 == console ]] || exit 1
if [[ -n ${LOCKED:-} && " $* " != *" -lock=false "* ]]; then
  echo "Error acquiring the state lock" >&2
  exit 1
fi
[[ -z ${BROKEN:-} ]] || { echo "Error: Failed to load state" >&2; exit 1; }
case $(cat) in
  var.name) echo '"iki-test-x"' ;;
  var.region) echo '"us-east-1"' ;;
  var.dr_region) echo '"us-west-2"' ;;
esac
"""


def teardown(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """teardown.sh up to its question, with fake tofu and aws: it is answered no."""
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "tofu").write_text(FAKE_TOFU)
    (tools / "aws").write_text("#!/usr/bin/env bash\necho 000000000000\n")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    (tmp_path / "deploy" / "aws" / "production" / ".terraform").mkdir(parents=True)
    return subprocess.run(  # noqa: S603 - the script under test
        [str(TEARDOWN), "iki-test-x"],
        input="no\n",
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(tmp_path), **env},
    )


def test_teardown_reads_its_variables_while_a_lock_is_held(tmp_path: Path) -> None:
    """A failed apply can still be letting go of its lock when teardown starts again: live
    run iki-test-11180346's retry stopped there, its deployment's name read as empty."""
    result = teardown(tmp_path, LOCKED="1")
    # Past its variables, to its question (asked silently without a terminal).
    assert result.stderr == ""
    assert result.stdout == "Nothing deleted.\n"


def test_teardown_stops_on_a_variable_it_cannot_read(tmp_path: Path) -> None:
    result = teardown(tmp_path, BROKEN="1")
    assert result.returncode != 0
    assert "Failed to load state" in result.stderr
    assert "Couldn't read var.name" in result.stderr
    assert "variables are for deployment" not in result.stderr
