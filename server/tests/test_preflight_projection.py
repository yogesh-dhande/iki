"""SQL checks use the scalar values a generated migration would store."""

import datetime as dt
import sys
from dataclasses import replace

import pytest
from django.db import connection
from psycopg import sql

from iki_spec.model import Field

pytestmark = pytest.mark.django_db


def test_default_unit_conversion_and_overflow_in_one_query() -> None:
    from iki_server.plugins.preflight_projection import project

    old = Field("mass", "quantity", unit="g")
    new = replace(old, unit="mg", required=True, has_default=True, default=2)
    expr = project("mass", {"mass": old}, {"mass": new}, sql.Identifier)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT {} FROM unnest(%s::float8[]) AS mass").format(expr),
            [[None, 0.0, 1.2345, sys.float_info.max, -sys.float_info.max]],
        )
        assert [value for (value,) in cursor.fetchall()] == [2000, 0, 1234.5, None, None]

    # PostgreSQL raises on floating-point underflow too. A single exceptional
    # value cannot prevent the count for the rest of the table.
    smaller = replace(old, unit="kg")
    expr = project("mass", {"mass": old}, {"mass": smaller}, sql.Identifier)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT {} FROM unnest(%s::float8[]) AS mass").format(expr),
            [[0.0, -1.5, float.fromhex("0x0.0000000000001p-1022")]],
        )
        assert [value for (value,) in cursor.fetchall()] == [0, -0.0015, None]


def test_state_renames_and_retirements_apply_after_initializing_new_fields() -> None:
    from iki_server.plugins.preflight_projection import project

    def state(name: str, data: dict[str, object]) -> Field:
        return Field.from_manifest(name, {"type": "state", **data}, plugin="acme/lab")

    ready = state("status", {"states": {"ready": {}, "waiting": {}}, "initial": "ready"})
    moved = state(
        "status",
        {
            "states": {"ready": {"removed": True, "moved_to": "done"}, "done": {}, "waiting": {}},
            "initial": "waiting",
        },
    )
    with connection.cursor() as cursor:
        expr = project("status", {"status": ready}, {"status": moved}, sql.Identifier)
        cursor.execute(
            sql.SQL("SELECT {} FROM unnest(%s::text[]) AS status").format(expr),
            [["ready", "waiting"]],
        )
        assert cursor.fetchall() == [("done",), ("waiting",)]
    retired = state(
        "status",
        {
            "initial": "ready",
            "states": {"ready": {}, "waiting": {}},
            "removed": True,
            "moved_to": {"field": "review", "states": {"ready": "done", "waiting": "pending"}},
        },
    )
    review = state("review", {"states": {"pending": {}, "done": {}}, "initial": "pending"})
    expr = project(
        "review", {"status": ready}, {"status": retired, "review": review}, sql.Identifier
    )
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT {} FROM unnest(%s::text[]) AS status").format(expr),
            [["ready", "waiting"]],
        )
        assert cursor.fetchall() == [("done",), ("pending",)]

    # Django generates the RemoveFields by name. A second retirement targeting
    # the same state takes effect in that order, regardless of manifest key order.
    earlier = replace(ready, name="earlier")
    assert retired.moved_to is not None
    left = replace(
        retired,
        name="earlier",
        moved_to=replace(retired.moved_to, states=(("ready", "pending"), ("waiting", "pending"))),
    )
    expr = project(
        "review",
        {"status": ready, "earlier": earlier},
        {"status": retired, "earlier": left, "review": review},
        sql.Identifier,
    )
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT {} FROM (VALUES ('ready','ready')) v(earlier,status)").format(expr)
        )
        assert cursor.fetchone() == ("done",)


def test_type_changes_and_defaults_leave_nonconvertible_values_unread() -> None:
    from iki_server.plugins.preflight_projection import project

    before = {"batch": Field("batch", "int")}
    after = {"batch": Field("batch", "float")}
    widened = project("batch", before, after, sql.Identifier)
    added = Field("lab", "string", required=True, has_default=True, default="Lab 'A'")
    literal = project("lab", before, {"lab": added}, sql.Identifier)
    incompatible = project("batch", before, {"batch": Field("batch", "date")}, sql.Identifier)
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT {}, {}, {} FROM (VALUES (3::bigint)) v(batch)").format(
                widened, literal, incompatible
            )
        )
        assert cursor.fetchone() == (3.0, "Lab 'A'", None)


@pytest.mark.parametrize(
    ("kind", "default"),
    [("float", {}), ("int", 2**63), ("date", "February"), ("enum", {"label": "x"})],
)
def test_invalid_scalar_defaults_raise_a_manifest_error(kind: str, default: object) -> None:
    from iki_server.plugins.preflight_projection import project

    field = Field("code", kind, required=True, has_default=True, default=default)
    with pytest.raises(ValueError, match="default"):
        project(field.name, {}, {field.name: field}, sql.Identifier)


def test_typed_defaults_include_datetime_list_items() -> None:
    from iki_server.plugins.preflight_projection import typed_default

    values = [
        (Field("whole", "int"), -(2**63)),
        (Field("flag", "bool"), False),
        (Field("day", "date"), "2024-02-29"),
        (Field("instant", "datetime"), "2024-02-29T02:03:04+02:00"),
        (Field("label", "enum", values=("A",)), "A"),
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("SELECT ") + sql.SQL(", ").join(typed_default(f, v) for f, v in values)
        )
        assert cursor.fetchone() == (
            -(2**63),
            False,
            dt.date(2024, 2, 29),
            dt.datetime(2024, 2, 29, 0, 3, 4, tzinfo=dt.UTC),
            "A",
        )
    with pytest.raises(ValueError, match="default"):
        typed_default(Field("instant", "datetime"), "2024-02-29T02:03:04")


def test_normalized_list_has_no_column_and_cannot_fill_a_scalar_default() -> None:
    from iki_server.plugins.preflight_projection import project

    name = "inputs"
    previous = Field(name, "list", items=Field("", "ref", to=("acme/lab.Stock",)))
    candidate = Field(name, "string", required=True, has_default=True, default="new")

    def no_column(_name: str) -> sql.Composable:
        pytest.fail("a normalized list has no scalar column")

    value = project(name, {name: previous}, {name: candidate}, no_column)
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("SELECT {}").format(value))
        assert cursor.fetchone() == (None,)
