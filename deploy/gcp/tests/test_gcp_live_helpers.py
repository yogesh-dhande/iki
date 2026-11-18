"""The live Google Cloud test's own helpers, checked offline: they need no project, so they
run everywhere, CI included, while test_gcp_live.py's checks themselves don't."""

import subprocess
import sys

import test_gcp_live as live


def test_a_stopped_process_is_reaped() -> None:
    """A process terminated but never waited for is still running as far as Python knows:
    pytest fails the whole live run over the ResourceWarning its Popen raises."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    live.stop(process)
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
        live.stop(process, grace=0.5)
    assert process.returncode is not None
