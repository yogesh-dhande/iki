"""The live tests' shared harness, checked offline: the token, run names, inventories, the
skip rules and the shape of a run's life. No cloud, no credentials."""

import datetime
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, ClassVar

import iki_live
import live_cost
import pytest


def command(tmp_path: Path, body: str) -> str:
    script = tmp_path / "mint.py"
    script.write_text(body)
    return f"{sys.executable} {script}"


def test_a_token_is_written_whole_and_private(tmp_path: Path) -> None:
    token = iki_live.Token(command(tmp_path, "print('chatter'); print('ya29.a0-token_value')"), "T")
    try:
        token.mint(attempts=1)
        assert token.path.read_text() == "ya29.a0-token_value"
        # Only this user may enter the directory or read the file.
        assert stat.S_IMODE(token.dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(token.path.stat().st_mode) == 0o600
        assert not token.path.with_name("token.new").exists()
        assert token.read() == "ya29.a0-token_value"
    finally:
        token.stop()
    assert not token.dir.exists()


def test_a_jwt_is_a_token(tmp_path: Path) -> None:
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJzdHMuYW1hem9uYXdzLmNvbSJ9.c2lnbmF0dXJl"
    token = iki_live.Token(command(tmp_path, f"print({jwt!r})"), "T")
    try:
        token.mint(attempts=1)
        assert token.path.read_text() == jwt
    finally:
        token.stop()


def test_a_failed_mint_names_the_variable_never_the_output(tmp_path: Path) -> None:
    secret = "ya29.leaked-if-printed"
    script = f"import sys; print({secret!r}); print('oops', file=sys.stderr); sys.exit(3)"
    token = iki_live.Token(command(tmp_path, script), "IKI_X_TOKEN_COMMAND")
    try:
        with pytest.raises(iki_live.TokenError) as raised:
            token.mint(attempts=2, wait=0)
        assert "IKI_X_TOKEN_COMMAND" in str(raised.value) and "exit 3" in str(raised.value)
        assert secret not in str(raised.value)
        assert not token.path.exists()
    finally:
        token.stop()


def test_a_line_that_isnt_a_token_isnt_written(tmp_path: Path) -> None:
    token = iki_live.Token(command(tmp_path, "print('ERROR: (gcloud) not signed in')"), "T")
    try:
        with pytest.raises(iki_live.TokenError):
            token.mint(attempts=1)
    finally:
        token.stop()


def test_a_stale_token_stops_the_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = iki_live.Token(command(tmp_path, "print('abc')"), "T")
    try:
        token.mint(attempts=1)
        token.minted -= token.MAX_AGE + 1
        with pytest.raises(iki_live.TokenError):
            token.read()
        tools = iki_live.Tools(tmp_path, token, {}, set())
        with pytest.raises(iki_live.TokenError):
            tools.run("true")
    finally:
        token.stop()


def test_a_reader_never_sees_a_token_half_written(tmp_path: Path) -> None:
    path = tmp_path / "token"
    iki_live.write_private(path, "a" * 4096)
    seen: set[int] = set()
    done = threading.Event()

    def read() -> None:
        while not done.is_set():
            seen.add(len(path.read_text()))

    reader = threading.Thread(target=read)
    reader.start()
    for i in range(200):
        iki_live.write_private(path, ("b" if i % 2 else "a") * 4096)
    done.set()
    reader.join()
    assert seen == {4096}


def test_the_tools_drop_the_callers_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("KUBECONFIG", "/home/me/.kube/config")
    token = iki_live.Token(command(tmp_path, "print('abc')"), "T")
    try:
        token.mint(attempts=1)
        tools = iki_live.Tools(tmp_path, token, {"EXTRA": "1"}, {"AWS_ACCESS_KEY_ID", "KUBECONFIG"})
        env = tools._env()
        assert "AWS_ACCESS_KEY_ID" not in env
        assert env["KUBECONFIG"] == str(tmp_path / "kubeconfig")
        assert env["EXTRA"] == "1" and env["IKI_TEARDOWN_YES"] == "1"
        out = tools.run(sys.executable, "-c", "import os; print(os.environ.get('EXTRA'))").stdout
        assert out.strip() == "1"
    finally:
        token.stop()


def test_a_new_run_has_a_name_of_its_own_and_a_later_look_finds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(iki_live, "REPO", tmp_path)
    name = iki_live.run_name("aws", "", new=True)
    assert name.startswith("iki-test-") and len(name) == len("iki-test-") + 8
    assert iki_live.run_name("aws", "", new=False) == name
    assert iki_live.run_name("aws", "iki-test-mine", new=True) == "iki-test-mine"
    assert (tmp_path / "build/aws-live/last-run").read_text() == name


def test_inventories_are_compared_by_section() -> None:
    before = {"vpcs": {"vpc-1"}, "roles": {"a", "b"}}
    after = {"vpcs": {"vpc-1", "vpc-2"}, "roles": {"a"}, "keys": {"k"}}
    assert iki_live.added(before, after) == {"vpcs": {"vpc-2"}, "keys": {"k"}}
    assert iki_live.removed(before, after) == {"roles": {"b"}}


def test_an_inventory_is_written_sorted(tmp_path: Path) -> None:
    iki_live.write_sections(tmp_path / "i.txt", {"b": {"z", "y"}, "a": set()}, ["=== tail", "x"])
    assert (tmp_path / "i.txt").read_text() == "=== b\ny\nz\n\n=== a\n\n=== tail\nx\n"


class Config:
    def __init__(self, flag: bool) -> None:
        self.flag = flag

    def getoption(self, name: str) -> bool:
        return self.flag


@pytest.mark.parametrize(
    ("env", "flag", "expected"),
    [
        ({"CI": "true", "IKI_X": "1"}, True, "never runs on CI"),
        ({"IKI_X": "1"}, False, "run it with --x-live"),
        ({}, True, "needs IKI_X"),
        ({"IKI_X": "1"}, True, None),
    ],
)
def test_a_live_test_runs_only_when_asked_and_never_on_ci(
    env: dict[str, str], flag: bool, expected: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("IKI_X", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    reason = iki_live.skip_reason(
        Config(flag),  # type: ignore[arg-type]
        cloud="X",
        flag="--x-live",
        required={"IKI_X": "the account"},
    )
    assert reason is None if expected is None else expected in (reason or "")


class Fake(iki_live.Live):
    """A deployment whose steps only say what was done."""

    def __init__(self, tmp_path: Path, tools: iki_live.Tools, fail: str = "") -> None:
        super().__init__("x", "iki-test-x", tmp_path, tools, "1")
        self.done: list[str] = []
        self.fail = fail

    def step(self, what: str) -> None:
        self.done.append(what)
        if what == self.fail:
            raise RuntimeError(what)

    def inventory(self) -> Any:
        self.done.append("inventory")

        class Inventory:
            def write(self, path: Path) -> None:
                path.write_text("")

        return Inventory()

    def preconditions(self) -> None:
        self.step("preconditions")

    def stand_up(self) -> None:
        self.step("stand up")

    def diagnose(self) -> None:
        self.done.append("diagnose")

    def teardown_steps(self) -> list[tuple[str, Any]]:
        return [("teardown", lambda: self.step("teardown"))]

    def leftovers(self, before: Any, after: Any) -> list[str]:
        return []

    def cost_report(self, start: datetime.datetime) -> str:
        self.cost_usd = 1.5
        return "cost\n"


def fake_tools(tmp_path: Path) -> iki_live.Tools:
    return iki_live.Tools(
        tmp_path, iki_live.Token(command(tmp_path, "print('abc')"), "T"), {}, set()
    )


class Request:
    """What lifecycle reads of pytest's request: nothing ran, so nothing is recorded."""

    class node:  # noqa: N801 - pytest's attribute
        obj = sys.modules[__name__]

    class session:  # noqa: N801
        items: ClassVar[list[Any]] = []
        stash: ClassVar[dict[Any, Any]] = {}


def test_standing_up_fails_then_it_diagnoses_and_still_tears_down(tmp_path: Path) -> None:
    d = Fake(tmp_path, fake_tools(tmp_path), fail="stand up")
    life = iki_live.lifecycle(d, Request)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="stand up"):
        next(life)
    assert d.done == ["inventory", "preconditions", "stand up", "diagnose", "teardown", "inventory"]
    assert not d.tools.token.dir.exists()


def test_a_teardown_step_that_fails_doesnt_stop_the_rest(tmp_path: Path) -> None:
    d = Fake(tmp_path, fake_tools(tmp_path), fail="teardown")
    life = iki_live.lifecycle(d, Request)  # type: ignore[arg-type]
    next(life)
    with pytest.raises(pytest.fail.Exception, match="teardown: teardown"):
        next(life, None)
    assert d.done[-1] == "inventory"


def test_preconditions_that_fail_make_nothing(tmp_path: Path) -> None:
    d = Fake(tmp_path, fake_tools(tmp_path), fail="preconditions")
    with pytest.raises(RuntimeError):
        next(iki_live.lifecycle(d, Request))  # type: ignore[arg-type]
    assert "stand up" not in d.done and "teardown" not in d.done


def test_a_stopped_process_is_reaped() -> None:
    """A process terminated but never waited for is still running as far as Python knows:
    pytest fails the whole live run over the ResourceWarning its Popen raises."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    iki_live.stop(process)
    assert process.returncode is not None


def test_a_process_that_ignores_terminate_is_killed() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            " print('ready', flush=True); time.sleep(600)",
        ],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    with process.stdout:
        process.stdout.readline()
        iki_live.stop(process, grace=0.5)
    assert process.returncode is not None


def test_the_cost_frameworks_lines_add_up() -> None:
    report = live_cost.Report(window={"name": "n", "start": "s", "end": "e"})
    report.lines += [
        live_cost.Line("a", 1, 1, "h", "S1", "d", 0.1, 0.1),
        live_cost.Line("b", 2, 1, "h", "S2", "d", 0.2, 0.4, note="why"),
    ]
    assert report.total == pytest.approx(0.5)
    assert "why" in report.text() and "0.5000" in report.text()
    assert live_cost.prorate(7.3, "GB-Mo", 10) == pytest.approx(0.1)
    assert live_cost.prorate(0.1, "Hrs", 10) == pytest.approx(1.0)


def test_os_environment_is_untouched_by_the_tools(tmp_path: Path) -> None:
    before = dict(os.environ)
    tools = fake_tools(tmp_path)
    tools.token.stop()
    assert dict(os.environ) == before
