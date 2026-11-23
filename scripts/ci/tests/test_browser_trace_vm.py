"""A VM submission can replay before its client fetches a result."""

import os
import subprocess
import tarfile
from pathlib import Path

WRAPPER = Path("scripts/ci/with-browser-traces.sh")
ROOT = Path(__file__).resolve().parents[3]


def test_vm_fetch_keeps_each_failed_attempt_and_identifies_its_terminal_result(
    tmp_path: Path,
) -> None:
    host = tmp_path / "host"
    remote = tmp_path / "vm"
    host.mkdir()
    (remote / WRAPPER).parent.mkdir(parents=True)
    (remote / WRAPPER).symlink_to(ROOT / WRAPPER)
    runner = tmp_path / "vm-run"
    # Like vm-run.sh, each replay receives the identical argv. Only the confirmed
    # end result and literal VM_RUN_FETCH paths come back to the client.
    runner.write_text(
        "#!/bin/sh\nset -eu\nstatus=0\n"
        "for probe in $PROBE_ATTEMPTS; do\n"
        '  status=0\n  (cd "$PROBE_REMOTE" && PROBE="$probe" "$@") || status=$?\ndone\n'
        "for file in $VM_RUN_FETCH; do\n"
        '  if [ "$PROBE_DROP_FETCH" != 1 ]; then\n'
        '    mkdir -p "$(dirname "$file")" && cp -p "$PROBE_REMOTE/$file" "$file"\n'
        '    if [ "${file##*.}" = gz ]; then\n'
        '      case $PROBE_DAMAGE in corrupt) printf damaged > "$file";;\n'
        '        missing-receipt) tar -czf "$file" -C "$PROBE_REMOTE" scripts/ci;; esac\n'
        '    fi\n  fi\ndone\nexit "$status"\n'
    )
    runner.chmod(0o755)
    gate = tmp_path / "gate"
    gate.write_text(
        "#!/bin/sh\nset -eu\ncase $PROBE in green) exit 0;; early) exit 23;;\n"
        '  interrupted) kill -KILL "$PPID"; exit 1;; esac\n'
        'mkdir -p "$IKI_BROWSER_ARTIFACTS/gw0/same-test"\n'
        'echo "$PROBE" > "$IKI_BROWSER_ARTIFACTS/gw0/same-test/context-1-trace.zip"\n'
        "exit 17\n"
    )
    gate.chmod(0o755)

    def submit(
        attempts: str, *, drop_fetch: bool = False, damage: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - repository wrapper and disposable runner
            [ROOT / WRAPPER, "--vm", runner, gate],
            cwd=host,
            env={
                **os.environ,
                "VM_RUN_FETCH": "",
                "PROBE_REMOTE": str(remote),
                "PROBE_ATTEMPTS": attempts,
                "PROBE_DROP_FETCH": str(int(drop_fetch)),
                "PROBE_DAMAGE": damage,
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    first = submit("first second green")
    assert first.returncode == 0, first.stderr
    root = host / "build/browser-failures"
    (bundle,) = root.glob("run-*/traces.tar.gz")
    original = bundle.read_bytes()
    with tarfile.open(bundle) as archive:
        (receipt,) = [m for m in archive if m.name == "collected-attempt.txt"]
        record = archive.extractfile(receipt)
        assert record is not None
        terminal = record.read().decode()
        assert bundle.parent.name in terminal and "exited 0 and recorded no" in terminal
        traces = [m for m in archive if m.name.endswith(".zip")]
        assert len(traces) == 2
        attempts = {Path(m.name).parts[0] for m in traces}
        assert len(attempts) == 2 and all(a.startswith("attempt-") for a in attempts)
        assert all(a not in terminal for a in attempts)
        bodies = []
        for trace in traces:
            source = archive.extractfile(trace)
            assert source is not None
            bodies.append(source.read())
        assert sorted(bodies) == [b"first\n", b"second\n"]

    second = submit("third")
    assert second.returncode == 17, second.stderr
    assert submit("green").returncode == 0
    assert submit("early").returncode == 23
    lost = submit("green", drop_fetch=True)
    assert lost.returncode == 125 and "No result archive returned" in lost.stderr
    assert len(list(root.glob("run-*/traces.tar.gz"))) == len(list(root.iterdir())) == 2
    assert bundle.read_bytes() == original
    interrupted = submit("before-interruption interrupted")
    assert interrupted.returncode != 0
    assert "no confirmed collection" in interrupted.stderr
    assert len(list(root.glob("run-*/traces.tar.gz"))) == 3
    assert bundle.read_bytes() == original
    damaged_cases = (("corrupt", "failure", 17), ("missing-receipt", "green", 125))
    for damage, outcome, expected in damaged_cases:
        damaged = submit(outcome, damage=damage)
        assert damaged.returncode == expected, damaged.stderr
        assert "Unconfirmed trace delivery" in damaged.stderr
    assert len(list(root.glob("run-*/traces.tar.gz"))) == 5
