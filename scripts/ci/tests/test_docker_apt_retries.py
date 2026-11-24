"""Every Dockerfile's apt invocations use bounded network retries and timeouts."""

import json
import re
import shlex
import subprocess
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[3]
RETRY_ARG = (
    'APT_RETRY="-o Acquire::Retries=8 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"'
)
RETRY_NAME = re.compile(r"(?:^|\s)APT_RETRY(?:\s|=|$)")
APT = re.compile(r"(?<![\w.-])apt-get(?![\w.-])")
NETWORK_OVERRIDE = re.compile(r"Acquire::(?:Retries|https?::Timeout)(?:=|$)", re.IGNORECASE)
SHELL_TOKEN = re.compile(r"""#.*|[&;|()]|(?:\\.|'[^']*'|"(?:[^"\\]|\\.)*"|[^\s\\'";&|()])+""")
SEPARATORS = {"&", ";", "|", "(", ")"}
DIRECTIVE = re.compile(r"\s*#\s*(syntax|check|escape)\s*=\s*(.*?)\s*", re.IGNORECASE)
HEREDOC = re.compile(r"<<(-?)([^<\s]+)")


def _heredocs(body: str) -> Iterator[tuple[str, bool]]:
    tokens = iter(SHELL_TOKEN.findall(body))
    for token in tokens:
        if token.startswith("#"):
            return
        if not token.startswith("<<"):
            continue
        if token in {"<<", "<<-"}:
            token += next(tokens, "")
        marker = HEREDOC.fullmatch(shlex.split(token)[0])
        if marker:
            yield marker[2], bool(marker[1])


def _instructions(text: str) -> Iterator[tuple[int, str, str]]:
    """Read ordinary Linux Dockerfile instructions, folding continued lines."""
    pending = ""
    start = 0
    heredocs: list[tuple[str, bool]] = []
    kind = ""
    for number, line in enumerate(text.splitlines(), 1):
        if heredocs:
            marker, strip_tabs = heredocs[0]
            if (line.lstrip("\t") if strip_tabs else line) == marker:
                heredocs.pop(0)
            elif kind == "RUN":
                yield number, "RUN_HEREDOC", line
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not pending:
            start = number
        line = line.rstrip()
        if line.endswith("\\"):
            pending += line[:-1]
            continue
        keyword, *arguments = (pending + line).split(None, 1)
        kind = keyword.upper()
        body = arguments[0].strip() if arguments else ""
        yield start, kind, body
        if kind in {"RUN", "COPY", "ADD"}:
            heredocs = list(_heredocs(body))
        pending = ""
    assert not pending, f"Line {start} ends with an incomplete Dockerfile instruction"
    assert not heredocs, f"Line {start} starts an unterminated Dockerfile heredoc"


def _shell_commands(body: str) -> Iterator[list[str]]:
    command: list[str] = []
    for token in SHELL_TOKEN.findall(body):
        if token.startswith("#"):
            break
        if token in SEPARATORS:
            if command:
                yield command
                command = []
        else:
            command.append(token)
    if command:
        yield command


def _run_issues(body: str, has_retry: bool) -> Iterator[str]:
    # RUN mounts and network flags are Docker options, not the executable.
    body = re.sub(r"^(?:--\S+\s+)+", "", body)
    if body.startswith("["):
        try:
            args = json.loads(body)
        except json.JSONDecodeError:
            # A shell's [ test can also start a RUN instruction.
            pass
        else:
            if (
                args
                and PurePosixPath(args[0]).name not in {"echo", "printf"}
                and any(APT.search(arg) for arg in args)
            ):
                yield "apt-get must use shell form so $APT_RETRY expands into separate flags"
            return
    for command in _shell_commands(body):
        words = [shlex.split(token)[0] for token in command]
        if words[0].startswith("APT_RETRY=") or (words[0] == "unset" and "APT_RETRY" in words[1:]):
            has_retry = False
        for index, word in enumerate(words):
            if PurePosixPath(word).name == "apt-get":
                if index and words[0] in {"echo", "printf"}:
                    continue
                if not has_retry or command[index + 1 : index + 2] != ["$APT_RETRY"]:
                    yield "declare the stage's canonical ARG APT_RETRY and use apt-get $APT_RETRY"
                elif any(NETWORK_OVERRIDE.search(option) for option in words[index + 2 :]):
                    yield "keep the retry and timeout values from $APT_RETRY"
            elif APT.search(word) and (
                words[0] not in {"echo", "printf"} or "$(" in word or "\x60" in word
            ):
                yield "use apt-get $APT_RETRY directly in a shell RUN so its flags can be checked"


def _apt_issues(text: str) -> list[tuple[int, str]]:
    for line in text.splitlines():
        directive = DIRECTIVE.fullmatch(line)
        if not directive:
            break
        if directive[1].lower() == "escape" and directive[2] != "\\":
            return [
                (number, "use the default Dockerfile escape character with apt-get")
                for number, line in enumerate(text.splitlines(), 1)
                if APT.search(line) and not line.lstrip().startswith("#")
            ]
    findings: list[tuple[int, str]] = []
    in_stage = configured = overridden = False
    for number, command, body in _instructions(text):
        if command == "FROM":
            in_stage, configured, overridden = True, False, False
        elif command == "ARG" and in_stage and RETRY_NAME.search(body):
            configured = body == RETRY_ARG
        elif command == "ENV" and RETRY_NAME.search(body):
            overridden = True
        elif command == "RUN":
            findings.extend(
                (number, issue) for issue in _run_issues(body, configured and not overridden)
            )
        elif command == "RUN_HEREDOC" and list(_run_issues(body, False)):
            findings.append((number, "use apt-get $APT_RETRY directly in a shell RUN"))
        elif command == "ONBUILD" and re.match(r"RUN\s", body, re.IGNORECASE) and APT.search(body):
            findings.append((number, "ONBUILD apt-get has no verified stage-local ARG APT_RETRY"))
    return findings


def test_tracked_dockerfiles_bound_apt_retries() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*Dockerfile*"],  # noqa: S607 - Git on PATH
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = [
        ROOT / name
        for name in result.stdout.split("\0")
        if name
        and (
            Path(name).name == "Dockerfile"
            or Path(name).name.startswith("Dockerfile.")
            or Path(name).name.endswith(".Dockerfile")
        )
    ]
    assert paths
    issues = [
        f"{path.relative_to(ROOT)}:{line}: {issue}"
        for path in paths
        for line, issue in _apt_issues(path.read_text(encoding="utf-8"))
    ]
    assert not issues, "\n" + "\n".join(issues)


@pytest.mark.parametrize(
    ("body", "issue_count"),
    [
        (f"FROM debian\nARG {RETRY_ARG}\nRUN apt-get $APT_RETRY update", 0),
        (f"ARG {RETRY_ARG}\nFROM debian\nRUN apt-get $APT_RETRY update", 1),
        (f"FROM debian\n# ARG {RETRY_ARG}\nRUN apt-get $APT_RETRY update", 1),
        (f"FROM debian\nRUN apt-get $APT_RETRY update\nARG {RETRY_ARG}", 1),
        (
            f"FROM debian AS unused\nARG {RETRY_ARG}\nFROM debian\nRUN apt-get $APT_RETRY update",
            1,
        ),
        (f'FROM debian\nARG {RETRY_ARG}\nRUN apt-get "$APT_RETRY" update', 1),
        (f'FROM debian\nARG {RETRY_ARG}\nRUN ["apt-get", "$APT_RETRY", "update"]', 1),
        (f'FROM debian\nARG {RETRY_ARG}\nRUN ["sh", "-c", "apt-get $APT_RETRY update"]', 1),
        (r"FROM debian" "\n" r'RUN ["\u0061pt-get", "update"]', 1),
        (f"FROM debian\nARG {RETRY_ARG}\nRUN [ -d /usr ] && apt-get $APT_RETRY update", 0),
        (f"FROM debian\nARG {RETRY_ARG}\nONBUILD RUN apt-get $APT_RETRY update", 1),
        (
            f"FROM debian\nARG {RETRY_ARG}\nENV APT_RETRY=''\nRUN apt-get $APT_RETRY update",
            1,
        ),
        (f"FROM debian\nARG {RETRY_ARG}\nRUN APT_RETRY=''; apt-get $APT_RETRY update", 1),
        (
            f"FROM debian\nARG {RETRY_ARG}\nRUN apt-get $APT_RETRY -o Acquire::Retries=0 update",
            1,
        ),
        ("FROM debian\nRUN\tapt-get update", 1),
        ('FROM debian\n# RUN apt-get upgrade\nRUN echo "apt-get update"', 0),
        (
            f"""\
            FROM debian
            ARG {RETRY_ARG}
            RUN apt-get $APT_RETRY update \\
            # A Dockerfile comment in a continuation
             && apt-get install -y curl
            """,
            1,
        ),
        (f"FROM debian\nARG {RETRY_ARG}\nRUN echo test # apt-get install foo", 0),
        (f"FROM debian\nARG {RETRY_ARG}\nRUN <<'EOF'\napt-get update\nEOF", 1),
        (f"FROM debian\nCOPY <<EOF /note\nARG {RETRY_ARG}\nEOF\nRUN apt-get $APT_RETRY update", 1),
        ("# escape=\x60\nFROM debian\nRUN true && \x60\n    apt-get update", 1),
    ],
)
def test_apt_checks_follow_the_current_stage_and_each_command(body: str, issue_count: int) -> None:
    assert len(_apt_issues(dedent(body))) == issue_count
