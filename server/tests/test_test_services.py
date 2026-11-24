"""A missing test service must name the image and the alternative external URL."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest
from conftest import POSTGRES_IMAGE, SEAWEEDFS_IMAGE, _postgres_server, _seaweedfs_server


@pytest.mark.parametrize(
    ("server", "constructor", "image", "external"),
    [
        (
            _postgres_server,
            "testcontainers.community.postgres.PostgresContainer",
            POSTGRES_IMAGE,
            "IKI_TEST_DATABASE_URL",
        ),
        (
            _seaweedfs_server,
            "testcontainers.core.container.DockerContainer",
            SEAWEEDFS_IMAGE,
            "IKI_TEST_S3_URL",
        ),
    ],
)
def test_container_start_failure(
    server: Callable[[], AbstractContextManager[str]],
    constructor: str,
    image: str,
    external: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(constructor, unavailable)
    monkeypatch.setenv(external, "http://provided.example")
    with server() as url:
        assert url == "http://provided.example"
    monkeypatch.delenv(external)
    with pytest.raises(RuntimeError, match="daemon unavailable") as failed, server():
        pytest.fail("an unavailable test container was reported as ready")
    notes = " ".join(getattr(failed.value, "__notes__", []))
    assert f"docker pull {image}" in notes and external in notes
