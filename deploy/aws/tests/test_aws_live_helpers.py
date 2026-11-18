"""The live AWS test's own helpers, checked offline: they need no account, so they run
everywhere, CI included, while test_aws_live.py's checks themselves don't."""

import json
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
