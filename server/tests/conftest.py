import os
import socket
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
import pytest_django
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from iki_server.observability.telemetry import configure_telemetry

if TYPE_CHECKING:
    from django.test import Client
    from fake_idp import FakeIdP
    from fake_saml_idp import FakeSamlIdP

# Same major version as production; pinned by digest so tests are reproducible.
POSTGRES_IMAGE = (
    "postgres:18.6-trixie@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"
)

#: The development and CI object store (ADR 0022), pinned like deploy/dev/manifests/seaweedfs.yaml.
SEAWEEDFS_IMAGE = (
    "chrislusf/seaweedfs:4.52"
    "@sha256:57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59"
)
S3_ACCESS_KEY = "ikitestaccesskey0001"
#: Each parallel worker's bucket of its own: servers under test set its CORS rule.
S3_BUCKET = f"iki-{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"
S3_SECRET_KEY = "ikitest-only-secret-key-not-used-anywhere"

SPAN_EXPORTER = InMemorySpanExporter()


#: What the web app's build manifest names, for tests that don't need the real app.
FAKE_WEB_APP_MANIFEST = {
    "src/main.tsx": {
        "file": "assets/main-test.js",
        "src": "src/main.tsx",
        "isEntry": True,
        "imports": ["_vendor-test.js"],
    },
    "_vendor-test.js": {"file": "assets/vendor-test.js"},
}


@pytest.fixture(autouse=True)
def _web_app_manifest(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages load a stand-in app, so the tests don't depend on a ``pnpm build``.

    Mark a test ``built_web_app`` to load the real one (browser tests).
    """
    if request.node.get_closest_marker("built_web_app"):
        return
    from iki_server.web import frontend

    monkeypatch.setattr(frontend, "manifest", lambda: FAKE_WEB_APP_MANIFEST)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--scale",
        action="store_true",
        help="Run the load tests (marked scale), such as importing 10M entities: "
        "`make check-scale`.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--scale"):
        return
    skip = pytest.mark.skip(reason="a load test: run it with --scale (make check-scale)")
    for item in items:
        if "scale" in item.keywords:
            item.add_marker(skip)


#: The servers the controller started for its workers, stopped when the run ends.
_SHARED = ExitStack()


def _runs_workers(config: pytest.Config) -> bool:
    """Whether this is pytest-xdist's controller, about to start workers (``-n``)."""
    return (
        "PYTEST_XDIST_WORKER" not in os.environ
        and bool(getattr(config.option, "numprocesses", None))
        and getattr(config.option, "dist", "no") != "no"
    )


def pytest_configure(config: pytest.Config) -> None:
    from django.conf import settings

    configure_telemetry(
        service_version=settings.IKI_VERSION,
        environment=settings.IKI_ENVIRONMENT,
        span_processor=SimpleSpanProcessor(SPAN_EXPORTER),
    )
    from django.db.models.signals import post_migrate

    post_migrate.connect(_seed_after_flush, dispatch_uid="iki-tests-seed-after-flush")
    if _runs_workers(config):
        # Workers share one Postgres and one object store, each test database and key
        # prefix its own, rather than start a server each. They inherit these.
        if "IKI_TEST_DATABASE_URL" not in os.environ:
            os.environ["IKI_TEST_DATABASE_URL"] = _SHARED.enter_context(_postgres_server())
        if "IKI_TEST_S3_URL" not in os.environ:
            os.environ["IKI_TEST_S3_URL"] = _SHARED.enter_context(_seaweedfs_server())


#: Data migrations that make rows every deployment has: the first-party OAuth clients.
SEEDING_MIGRATIONS = (
    "iki_server.oauth.migrations.0002_iki_cli",
    "iki_server.oauth.migrations.0003_iki_worker",
)


def _seed_after_flush(sender: Any, app_config: Any, **kwargs: Any) -> None:
    """Recreates the rows data migrations made, once a transactional test's flush has
    emptied every table: flush sends post_migrate after it. Run one at a time,
    pytest-django puts those tests last. In parallel, a file's tests run together, so
    other files' tests follow them on the same worker."""
    if app_config.label != "iki_oauth":
        return
    import importlib

    from django.apps import apps

    for name in SEEDING_MIGRATIONS:
        importlib.import_module(name).create(apps, None)


def pytest_unconfigure(config: pytest.Config) -> None:
    _SHARED.close()


#: Postgres's settings for the tests' own server. Sized as a deployment's database is, not
#: Postgres's defaults: the load tests write gigabytes (make check-scale). Each parallel
#: worker has a database of its own, and its servers and runners connect to it too.
POSTGRES_SETTINGS = (
    "-c shared_buffers=512MB -c max_wal_size=4GB -c work_mem=64MB -c max_connections=500"
)


@contextmanager
def _postgres_server() -> Iterator[str]:
    """A throwaway Postgres container, removed afterwards."""
    from testcontainers.community.postgres import PostgresContainer

    container = PostgresContainer(POSTGRES_IMAGE, username="iki", password="iki", dbname="iki")
    container.with_command(POSTGRES_SETTINGS)
    with container as pg:
        yield pg.get_connection_url(driver=None)


@pytest.fixture(scope="session")
def _postgres_url() -> Iterator[str]:
    """A Postgres server for the test run.

    CI provides one through ``IKI_TEST_DATABASE_URL``, and so does the controller of a
    parallel run (``-n``) for its workers. Otherwise a throwaway container is started
    with Docker and removed at the end of the session.
    """
    url = os.environ.get("IKI_TEST_DATABASE_URL")
    if url:
        yield url
        return
    with _postgres_server() as url:
        yield url


@pytest.fixture(scope="session")
def django_db_modify_db_settings(_postgres_url: str, request: pytest.FixtureRequest) -> None:
    from django.conf import settings

    parts = urlsplit(_postgres_url)
    # Update in place: Django's connection handler holds this same dict.
    settings.DATABASES["default"].update(
        HOST=parts.hostname,
        PORT=parts.port,
        USER=parts.username,
        PASSWORD=parts.password,
        NAME=parts.path.lstrip("/"),
    )
    # Then each parallel worker's test database is named after it (test_iki_gw0, ...),
    # as pytest-django's own fixture, which this replaces, would.
    request.getfixturevalue("django_db_modify_db_settings_parallel_suffix")


def allow_test_flush() -> None:
    """Lets pytest-django empty the audit table between transactional tests.

    The audit log refuses TRUNCATE from everyone; only the throwaway test
    database turns that one trigger off. ``test_audit`` checks it in place.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE iki_audit_event DISABLE TRIGGER iki_audit_event_no_truncate")
        cursor.execute("ALTER TABLE iki_audit_agent DISABLE TRIGGER iki_audit_agent_no_truncate")
        # Revisions and plugin history refuse TRUNCATE the same way.
        cursor.execute(
            "SELECT tgrelid::regclass::text FROM pg_trigger"
            " WHERE tgname = 'iki_history_no_truncate'"
        )
        for (table,) in cursor.fetchall():
            cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER iki_history_no_truncate")


@pytest.fixture(scope="session")
def django_db_setup(
    django_db_setup: None, django_db_blocker: pytest_django.DjangoDbBlocker
) -> Iterator[None]:
    """Ends stray connections before pytest-django drops the test database.

    Async tests run database checks in asgiref's worker thread. The test
    client keeps those connections open, which would block ``DROP DATABASE``.
    """
    with django_db_blocker.unblock():
        allow_test_flush()
        from iki_server.plugins import sync

        sync.sync()  # HRID sequences for the fixture plugins
    yield
    from django.db import connection

    with django_db_blocker.unblock(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    SPAN_EXPORTER.clear()
    yield SPAN_EXPORTER
    SPAN_EXPORTER.clear()


@pytest.fixture
def fake_idp(settings: Any) -> Iterator["FakeIdP"]:
    """A running fake OpenID Connect provider, reachable over plain HTTP.

    Iki only accepts http:// providers when IKI_HTTPS is off, so this
    fixture turns that check off (requests still use the https scheme).
    """
    from fake_idp import FakeIdP, serve

    from iki_server.web import csp

    settings.HTTPS = False
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    idp = FakeIdP(port=port)
    server, thread = serve(idp)
    csp.clear_cache()
    yield idp
    server.shutdown()
    server.server_close()
    thread.join()
    csp.clear_cache()


@pytest.fixture
def saml_idp(settings: Any) -> Iterator["FakeSamlIdP"]:
    """A running fake SAML identity provider serving its metadata over plain HTTP."""
    from fake_saml_idp import FakeSamlIdP, serve

    from iki_server.web import csp

    settings.HTTPS = False
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    idp = FakeSamlIdP(port=port)
    server, thread = serve(idp)
    csp.clear_cache()
    yield idp
    server.shutdown()
    server.server_close()
    thread.join()
    csp.clear_cache()


@pytest.fixture(scope="session")
def _s3_endpoint() -> Iterator[str]:
    """An S3-compatible store for the test run: a throwaway SeaweedFS, started on first use.

    ``IKI_TEST_S3_URL`` names one instead, which must accept the test keys above. The
    controller of a parallel run (``-n``) starts one for its workers.
    """
    url = os.environ.get("IKI_TEST_S3_URL")
    if url:
        yield url
        return
    with _seaweedfs_server() as url:
        yield url


@pytest.fixture(scope="session")
def _s3_bucket(_s3_endpoint: str) -> str:
    """The worker's bucket in the test store (S3_BUCKET), made if it isn't there."""
    import boto3

    s3 = boto3.client(
        "s3",
        endpoint_url=_s3_endpoint,
        region_name="us-east-1",
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
    )
    with suppress(s3.exceptions.BucketAlreadyOwnedByYou, s3.exceptions.BucketAlreadyExists):
        s3.create_bucket(Bucket=S3_BUCKET)
    return S3_BUCKET


@contextmanager
def _seaweedfs_server() -> Iterator[str]:
    """A throwaway SeaweedFS container, removed afterwards."""
    import time
    import urllib.request

    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(SEAWEEDFS_IMAGE)
        .with_command(
            "mini -dir=/data -ip=127.0.0.1 -ip.bind=0.0.0.0 -bucket=iki "
            "-admin.ui=false -master.telemetry=false"
        )
        .with_env("AWS_ACCESS_KEY_ID", S3_ACCESS_KEY)
        .with_env("AWS_SECRET_ACCESS_KEY", S3_SECRET_KEY)
        .with_exposed_ports(8333)
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8333)}"
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{url}/healthz", timeout=2):  # noqa: S310 - our own
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        # The store makes the bucket a moment after it starts answering.
        import boto3

        s3 = boto3.client(
            "s3",
            endpoint_url=url,
            region_name="us-east-1",
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
        )
        while True:
            try:
                s3.head_bucket(Bucket="iki")
                break
            except s3.exceptions.ClientError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield url


@pytest.fixture
def s3(
    _s3_endpoint: str, _s3_bucket: str, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """File storage, configured: Iki's settings point at the test store, under a prefix of
    the test's own. Yields a client for looking at what's stored."""
    import uuid

    from iki_server.files import storage

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", S3_ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", S3_SECRET_KEY)
    settings.IKI_FILES_BUCKET = _s3_bucket
    settings.IKI_FILES_PREFIX = f"test-{uuid.uuid4().hex}"
    settings.IKI_FILES_ENDPOINT_URL = _s3_endpoint
    settings.IKI_FILES_PUBLIC_ENDPOINT_URL = _s3_endpoint
    storage.reset()
    yield storage.client()
    storage.reset()


@pytest.fixture
def client() -> "Client":
    """A test client that behaves like a browser: https, and CSRF checks enforced."""
    from browser import Browser

    return Browser()


@pytest.fixture(autouse=True)
def _one_rate_limit_window(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each test's rate-limited requests in a single one-minute window.

    Limits count in fixed windows. A test that makes `limit` requests and
    expects the next one refused would otherwise pass or fail depending on
    whether a minute boundary fell between them. Tests of the windows
    themselves opt out with `@pytest.mark.real_rate_limit_clock`.
    """
    if request.node.get_closest_marker("real_rate_limit_clock"):
        return
    from django.utils import timezone

    from iki_server.web import ratelimit

    pinned = timezone.now().replace(second=30, microsecond=0)

    class _Clock:
        @staticmethod
        def now() -> datetime:
            return pinned

    monkeypatch.setattr(ratelimit, "timezone", _Clock)


@pytest.fixture(autouse=True)
def _close_worker_thread_connections() -> Iterator[None]:
    """Closes database connections that requests opened outside the main thread.

    Django's test clients don't close connections when a request finishes
    (that would close the test's own). Async requests run database code in
    asgiref's worker threads, so their connections would stay open, outside
    the test transaction, until garbage collection.
    """
    import threading

    from django.db.backends.signals import connection_created

    opened: list[Any] = []

    def record(sender: object, connection: Any, **kwargs: object) -> None:
        if threading.current_thread() is not threading.main_thread():
            opened.append(connection)

    connection_created.connect(record, weak=False)
    yield
    connection_created.disconnect(record)
    for wrapper in opened:
        wrapper.inc_thread_sharing()
        try:
            wrapper.close()
        finally:
            wrapper.dec_thread_sharing()
