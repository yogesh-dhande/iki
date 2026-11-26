"""The export sandbox (ADR 0230): where plugins' writers run, away from Iki's credentials.

    python -I -m iki_spec.writer_sandbox --port 8090 --health-port 8091 --concurrency 4

Its pod has no Secrets, no service-account token, no cloud identity and no network but the
connections web and tasks pods make to it. Each connection is one export. The launcher
hands it to a fresh child process (:mod:`iki_spec.writers.child`), started with an empty
environment and nothing open but its pipes, and relays frames between the two until the
child exits; it reads only the export's header, for its deadline, and never a record.
A child serves one export and is never used again. A couple are kept started and
waiting, so an export doesn't wait for Python and Wasmtime to load.

More exports than ``--concurrency`` at once are turned away (``busy``), and the server
answers 503 with ``Retry-After``. An export past its deadline has its child killed.

``/healthz/live`` on the health port says the launcher is up; ``/healthz/ready`` says every
writer the image installed loads and checks out, from a child that tried each, so an image
with a bad module never becomes ready and doesn't roll out.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import IO, Any, BinaryIO, ClassVar, cast

from iki_spec.writers import IMAGE
from iki_spec.writers.protocol import Closed, read_frame, write_frame, write_json

logger = logging.getLogger("iki.writer_sandbox")

#: The longest an export may run, whatever its header asks for.
MAX_DEADLINE = 30 * 60
#: Children kept started and waiting.
WAITING = 2


def child_command(modules: Path | None) -> list[str]:
    """How a child starts: isolated Python, the image's writers or a local module."""
    where = ["--modules", str(modules)] if modules is not None else ["--local"]
    return [sys.executable, "-I", "-m", "iki_spec.writers.child", *where]


def start_child(modules: Path | None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - this interpreter, Iki's own module
        child_command(modules),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
        close_fds=True,
        cwd="/",
    )


class Pool:
    """Children started ahead, each used once."""

    def __init__(self, modules: Path | None, waiting: int = WAITING) -> None:
        self.modules = modules
        self._ready: queue.Queue[subprocess.Popen[bytes]] = queue.Queue()
        self._wanted = threading.Semaphore(0)
        self._closed = threading.Event()
        for _ in range(waiting):
            self._wanted.release()
        self._filler = threading.Thread(target=self._fill, daemon=True, name="pool")
        self._filler.start()

    def _fill(self) -> None:
        while True:
            self._wanted.acquire()
            if self._closed.is_set():
                return
            try:
                self._ready.put(start_child(self.modules))
            except OSError:
                logger.exception("writer_child_failed_to_start")
                time.sleep(1)
                self._wanted.release()

    def take(self) -> subprocess.Popen[bytes]:
        self._wanted.release()
        try:
            child = self._ready.get(timeout=10)
        except queue.Empty:
            return start_child(self.modules)
        return child if child.poll() is None else start_child(self.modules)

    def close(self) -> None:
        """Starts no more children, and ends those still waiting."""
        self._closed.set()
        self._wanted.release()  # wakes the filler, to see it's closed
        # A child it was starting goes in the queue first, so this ends it too.
        self._filler.join(timeout=15)
        while True:
            try:
                child = self._ready.get_nowait()
            except queue.Empty:
                return
            for pipe in (child.stdin, child.stdout):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def _relay(source: IO[bytes], sink: IO[bytes], done: threading.Event) -> None:
    """Copies frames from ``source`` to ``sink`` until either end closes."""
    try:
        while not done.is_set():
            write_frame(sink, read_frame(source))
    except (Closed, OSError, ValueError):
        pass
    finally:
        done.set()


def serve_export(
    client_in: BinaryIO, client_out: BinaryIO, pool: Pool, max_deadline: float
) -> None:
    """One export: the header, then frames both ways, until the child finishes or the
    deadline passes."""
    try:
        raw = read_frame(client_in, 64 * 1024)
        header = json.loads(raw)
        deadline = min(float(header.get("deadline") or max_deadline), max_deadline)
    except (Closed, ValueError, TypeError, AttributeError):
        with contextlib.suppress(OSError):
            write_json(client_out, {"error": {"code": "malformed", "message": "Bad header."}})
        return
    child = pool.take()
    assert child.stdin is not None and child.stdout is not None  # noqa: S101 - pipes
    done = threading.Event()
    down = threading.Thread(target=_relay, args=(client_in, child.stdin, done), daemon=True)
    up = threading.Thread(target=_relay, args=(child.stdout, client_out, done), daemon=True)
    try:
        write_frame(child.stdin, raw)
        down.start()
        up.start()
        if not done.wait(deadline):
            logger.warning("writer_deadline", extra={"writer": header.get("writer")})
            child.kill()
            # Nothing more comes from the child: then say why, on a stream no one else writes.
            up.join(timeout=2)
            with contextlib.suppress(OSError):
                write_json(
                    client_out,
                    {
                        "failed": {
                            "code": "deadline",
                            "message": "The writer ran past its deadline.",
                        }
                    },
                )
        else:
            # Let the child's last answer through before the connection closes.
            up.join(timeout=2)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            child.stdin.close()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        if up.ident is not None:  # started
            up.join(timeout=2)
        with contextlib.suppress(OSError):
            child.stdout.close()


class _Handler(socketserver.StreamRequestHandler):
    server: Sandbox

    def handle(self) -> None:
        sandbox = self.server
        if not sandbox.slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                write_json(
                    cast("BinaryIO", self.wfile),
                    {"error": {"code": "busy", "message": "The sandbox is busy. Try again."}},
                )
            return
        try:
            serve_export(
                cast("BinaryIO", self.rfile),
                cast("BinaryIO", self.wfile),
                sandbox.pool,
                sandbox.max_deadline,
            )
        finally:
            sandbox.slots.release()


class Sandbox(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        modules: Path | None,
        concurrency: int,
        max_deadline: float = MAX_DEADLINE,
    ) -> None:
        super().__init__(address, _Handler)
        self.slots = threading.BoundedSemaphore(concurrency)
        self.pool = Pool(modules)
        self.max_deadline = max_deadline

    def server_close(self) -> None:
        super().server_close()
        self.pool.close()


def verify(modules: Path) -> dict[str, str | None]:
    """Each installed writer and what stops it loading, from a child that tries each."""
    found = subprocess.run(  # noqa: S603 - this interpreter, Iki's own module
        [*child_command(modules), "--verify"],
        capture_output=True,
        env={},
        check=False,
        timeout=300,
        cwd="/",
    )
    try:
        result: dict[str, str | None] = json.loads(found.stdout)
    except ValueError:
        return {"*": f"The check didn't finish (exit {found.returncode})."}
    return result


class _Health(BaseHTTPRequestHandler):
    ready: ClassVar[dict[str, Any]] = {
        "ready": False,
        "problems": {"*": "Still checking the writers."},
    }

    def do_GET(self) -> None:
        if self.path == "/healthz/live":
            self._answer(200, {"live": True})
        elif self.path == "/healthz/ready":
            state = _Health.ready
            self._answer(200 if state["ready"] else 503, state)
        else:
            self._answer(404, {"error": "not found"})

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401 - http.server's
        pass


def _check(modules: Path) -> None:
    problems = {k: v for k, v in verify(modules).items() if v is not None}
    _Health.ready = {"ready": not problems, "problems": problems}
    if problems:
        logger.error("writers_unavailable", extra={"problems": problems})


#: How long a stopping sandbox lets the exports it has finish.
GRACE = 25


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Iki's export sandbox (ADR 0230).")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--health-port", type=int, default=8091)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--modules", type=Path, default=IMAGE)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - the pod's own address
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    health = ThreadingHTTPServer((args.host, args.health_port), _Health)
    threading.Thread(target=health.serve_forever, daemon=True, name="health").start()
    threading.Thread(target=_check, args=(args.modules,), daemon=True, name="verify").start()
    with Sandbox((args.host, args.port), modules=args.modules, concurrency=args.concurrency) as box:

        def stop(_signal: int, _frame: object) -> None:
            # Take no more exports; serve_forever returns, and the ones running finish.
            threading.Thread(target=box.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        logger.info("sandbox_listening", extra={"port": args.port})
        box.serve_forever()
        deadline = time.monotonic() + GRACE
        taken = 0
        while taken < args.concurrency and time.monotonic() < deadline:
            if box.slots.acquire(timeout=max(0.0, deadline - time.monotonic())):
                taken += 1
    return 0


def connect(address: str, timeout: float) -> socket.socket:
    """A connection to the sandbox at ``tcp://host:port``."""
    if not address.startswith("tcp://"):
        raise ValueError(f"The sandbox's address is tcp://host:port, not {address}.")
    host, _, port = address.removeprefix("tcp://").rpartition(":")
    return socket.create_connection((host, int(port)), timeout=timeout)


#: Says the launcher has already re-executed itself with an empty environment. Not
#: ``os.environ`` being empty: Python's locale coercion (PEP 538) sets ``LC_CTYPE`` in a
#: process started with none, so that would re-execute forever.
CLEARED = "--environment-cleared"

if __name__ == "__main__":
    if sys.argv[1:2] == [CLEARED]:
        sys.exit(main(sys.argv[2:]))
    # Whatever the image set, the sandbox runs with nothing in its environment.
    os.execve(  # noqa: S606 - this interpreter, this module
        sys.executable,
        [sys.executable, "-I", "-m", "iki_spec.writer_sandbox", CLEARED, *sys.argv[1:]],
        {},
    )
