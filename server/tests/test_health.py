from unittest import mock

import pytest
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import AsyncClient

from iki_server import health

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _reset_migration_cache() -> None:
    health._MigrationState.applied = False


async def test_live_is_ok_without_touching_dependencies(async_client: AsyncClient) -> None:
    with mock.patch.object(health, "readiness", side_effect=AssertionError("not called")):
        response = await async_client.get(health.LIVE_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {}}
    assert response["Cache-Control"] == "no-store"


async def test_ready_is_ok_when_database_up_and_migrated(async_client: AsyncClient) -> None:
    response = await async_client.get(health.READY_PATH)
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "migrations": "ok"},
    }


async def test_ready_fails_without_leaking_details_when_database_down(
    async_client: AsyncClient,
) -> None:
    # Patch the class: the check runs in a worker thread with its own connection object.
    with mock.patch.object(
        type(connections["default"]), "cursor", side_effect=RuntimeError("password=hunter2")
    ):
        response = await async_client.get(health.READY_PATH)
    assert response.status_code == 503
    assert response.json() == {
        "status": "fail",
        "checks": {"database": "fail", "migrations": "fail"},
    }
    assert b"hunter2" not in response.content


def test_ready_fails_when_migrations_are_pending() -> None:
    with mock.patch.object(MigrationExecutor, "migration_plan", return_value=[("fake", False)]):
        assert health.check_migrations() == "fail"
    assert health._MigrationState.applied is False


def test_migration_success_is_cached() -> None:
    assert health.check_migrations() == "ok"
    with mock.patch.object(health, "MigrationExecutor", side_effect=AssertionError("rebuilt")):
        assert health.check_migrations() == "ok"


@pytest.mark.parametrize("path", [health.LIVE_PATH, health.READY_PATH])
async def test_probes_bypass_host_validation_and_https_redirect(
    async_client: AsyncClient, path: str
) -> None:
    # Kubelet probes use the pod IP as Host and plain HTTP.
    response = await async_client.get(path, secure=False, HTTP_HOST="10.1.2.3")
    assert response.status_code == 200


async def test_other_paths_still_get_the_https_redirect(async_client: AsyncClient) -> None:
    redirect = await async_client.get("/anything", secure=False)
    assert redirect.status_code == 301
    assert redirect["Location"] == "https://testserver/anything"


async def test_post_to_probe_path_is_not_a_probe(async_client: AsyncClient) -> None:
    response = await async_client.post(health.LIVE_PATH, secure=True)
    assert response.status_code == 404  # falls through to normal routing
