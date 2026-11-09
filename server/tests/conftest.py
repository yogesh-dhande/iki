import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
import pytest_django
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from iki_server.observability.telemetry import configure_telemetry

# Same major version as production; pinned by digest so tests are reproducible.
POSTGRES_IMAGE = (
    "postgres:18.6-trixie@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42"
)

SPAN_EXPORTER = InMemorySpanExporter()


def pytest_configure(config: pytest.Config) -> None:
    from django.conf import settings

    configure_telemetry(
        service_version=settings.IKI_VERSION,
        environment=settings.IKI_ENVIRONMENT,
        span_processor=SimpleSpanProcessor(SPAN_EXPORTER),
    )


@pytest.fixture(scope="session")
def _postgres_url() -> Iterator[str]:
    """A Postgres server for the test run.

    CI provides one through ``IKI_TEST_DATABASE_URL``. Locally, a throwaway
    container is started with Docker and removed at the end of the session.
    """
    url = os.environ.get("IKI_TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE, username="iki", password="iki", dbname="iki") as pg:
        yield pg.get_connection_url(driver=None)


@pytest.fixture(scope="session")
def django_db_modify_db_settings(_postgres_url: str) -> None:
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


@pytest.fixture(scope="session")
def django_db_setup(
    django_db_setup: None, django_db_blocker: pytest_django.DjangoDbBlocker
) -> Iterator[None]:
    """Ends stray connections before pytest-django drops the test database.

    Async tests run database checks in asgiref's worker thread. The test
    client keeps those connections open, which would block ``DROP DATABASE``.
    """
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
