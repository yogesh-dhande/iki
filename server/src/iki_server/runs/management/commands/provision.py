"""``iki-server provision``: starts machines for container- and vm-tier runs and tidies up
after them (ADR 0061, ADR 0074), until stopped (SIGTERM or Ctrl-C). It drives the container
tier when ``IKI_RUNS_NAMESPACE`` is set, and the vm tier when ``IKI_RUNS_VM_OWNER`` is.

Only one provisioner acts at a time: it holds a Postgres advisory lock, and others wait
for it. It runs a round every few seconds, and as soon as a job is queued on its tiers.
"""

import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any, ClassVar

import environ
import psycopg
import structlog
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import close_old_connections

from iki_server import health
from iki_server.observability.telemetry import configure_telemetry
from iki_server.runs import provisioner, queue
from iki_server.runs.ec2 import Boto3Ec2
from iki_server.runs.ec2_fleet import Ec2Fleet
from iki_server.runs.kubernetes import InCluster
from iki_server.tasks.runner import listen_connection

env = environ.Env()
logger = structlog.get_logger("iki.provisioner")

#: Seconds between rounds when nothing wakes it.
EVERY = 10.0
#: Any number that names this lock alone.
LOCK = 0x696B6970  # "ikip"


class Command(BaseCommand):
    help = "Start machines for container- and vm-tier runs, and tidy up after them."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--health-port",
            type=int,
            default=env.int("IKI_PROVISIONER_HEALTH_PORT", default=8081),
            help="Port for /healthz/live and /healthz/ready (0 for none).",
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401 - Django's signature
        configure_telemetry(
            service_version=settings.IKI_VERSION, environment=settings.IKI_ENVIRONMENT
        )
        loop = Loop(fleets())
        server = (
            HealthServer.start(loop, options["health_port"]) if options["health_port"] else None
        )
        try:
            loop.serve()
        finally:
            if server is not None:
                server.shutdown()


def _needs(*names: str) -> None:
    missing = [name for name in names if not getattr(settings, name)]
    if missing:
        raise CommandError(f"Set {', '.join(missing)} for the provisioner.")


def fleets() -> list[provisioner.Fleet]:
    """The machines this deployment starts: pods with the container tier, EC2 instances
    with the vm tier."""
    found: list[provisioner.Fleet] = []
    if settings.IKI_RUNS_NAMESPACE:
        _needs("IKI_RUNS_WORKER_IMAGE", "IKI_RUNS_SERVER_URL")
        host = env.str("KUBERNETES_SERVICE_HOST", "")
        port = env.str("KUBERNETES_SERVICE_PORT", "")
        if not host or not port:
            raise CommandError("The provisioner runs in a Kubernetes pod, with its own account.")
        found.append(provisioner.KubernetesFleet(InCluster(host, port)))
    if settings.IKI_RUNS_VM_OWNER:
        _needs("IKI_RUNS_WORKER_IMAGE", "IKI_RUNS_VM_SERVER_URL")
        found.append(Ec2Fleet(Boto3Ec2()))
    if not found:
        raise CommandError(
            "Set IKI_RUNS_NAMESPACE (the container tier) or IKI_RUNS_VM_OWNER (the vm tier) "
            "for the provisioner."
        )
    return found


class Loop:
    def __init__(self, machines: list[provisioner.Fleet]) -> None:
        self.machines = machines
        self.tiers = {fleet.tier for fleet in machines}
        self.stopping = threading.Event()
        #: When a round last finished, or the provisioner last checked for the lock.
        self.last_alive = time.monotonic()

    def alive(self) -> bool:
        return time.monotonic() - self.last_alive < 6 * EVERY

    def serve(self) -> None:
        def on_signal(signum: int, frame: FrameType | None) -> None:
            self.stopping.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        while not self.stopping.is_set():
            try:
                with listen_connection() as conn:
                    self._lead(conn)
            except psycopg.Error as error:
                logger.warning("provisioner_connection_lost", error=str(error))
                self.stopping.wait(5)

    def _lead(self, conn: psycopg.Connection[Any]) -> None:
        """Waits for the lock, then runs rounds while it holds it (as long as `conn`)."""
        while not self.stopping.is_set():
            row = conn.execute("SELECT pg_try_advisory_lock(%s)", [LOCK]).fetchone()
            self.last_alive = time.monotonic()
            if row and row[0]:
                break
            self.stopping.wait(EVERY)
        else:
            return
        logger.info("provisioner_leading")
        conn.execute(f"LISTEN {queue.CHANNEL}")
        while not self.stopping.is_set():
            self._round()
            for notify in conn.notifies(timeout=EVERY):
                if notify.payload in self.tiers:
                    break
                if self.stopping.is_set():
                    break

    def _round(self) -> None:
        try:
            started, ended = provisioner.tick(self.machines)
            if started or ended:
                logger.info("provisioner_round", started=started, ended=ended)
        except Exception:
            logger.exception("provisioner_round_failed")
        finally:
            close_old_connections()
        self.last_alive = time.monotonic()


class HealthServer(ThreadingHTTPServer):
    daemon_threads = True
    loop: Loop

    @classmethod
    def start(cls, loop: Loop, port: int) -> "HealthServer":
        server = cls(("0.0.0.0", port), _HealthHandler)  # noqa: S104 - probes use the pod IP
        server.loop = loop
        threading.Thread(
            target=server.serve_forever, name="iki-provision-health", daemon=True
        ).start()
        return server


class _HealthHandler(BaseHTTPRequestHandler):
    server: HealthServer
    server_version = "iki"
    sys_version = ""
    routes: ClassVar[set[str]] = {health.LIVE_PATH, health.READY_PATH}

    def do_GET(self) -> None:
        if self.path not in self.routes:
            self._answer(404, {"status": "not_found"})
            return
        if self.path == health.LIVE_PATH:
            alive = self.server.loop.alive()
            self._answer(200 if alive else 503, {"status": "ok" if alive else "fail"})
            return
        try:
            checks = health.readiness()
        finally:
            close_old_connections()
        healthy = all(result == "ok" for result in checks.values())
        self._answer(
            200 if healthy else 503, {"status": "ok" if healthy else "fail", "checks": checks}
        )

    def _answer(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ANN401
        """Probes every few seconds would flood the log."""
