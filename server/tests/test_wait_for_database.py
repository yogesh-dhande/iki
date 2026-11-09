from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.db import connections

# The command closes and reopens its connection, which needs real transactions.
pytestmark = pytest.mark.django_db(transaction=True)


def test_returns_once_the_database_answers() -> None:
    call_command("wait_for_database", "--timeout", "5")


def test_retries_until_the_database_comes_up() -> None:
    real_cursor = type(connections["default"]).cursor
    calls = {"n": 0}

    def flaky_cursor(self: object) -> object:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("starting up")
        return real_cursor(self)  # type: ignore[arg-type]

    with mock.patch.object(type(connections["default"]), "cursor", flaky_cursor):
        call_command("wait_for_database", "--timeout", "60", "--interval", "0.01")
    assert calls["n"] == 3


def test_gives_up_after_the_timeout() -> None:
    with (
        mock.patch.object(
            type(connections["default"]), "cursor", side_effect=RuntimeError("password=x")
        ),
        pytest.raises(CommandError, match="not reachable after 0s") as excinfo,
    ):
        call_command("wait_for_database", "--timeout", "0", "--interval", "0.01")
    assert "password" not in str(excinfo.value)
