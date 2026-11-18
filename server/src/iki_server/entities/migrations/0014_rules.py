"""Plugins' rules (ADR 0127): the rows the database holds ``unique`` and ``no_overlap``
rules with, records held by a rule a save broke, records found to break one, and what
each type's records were last checked against.

``sync_plugins`` puts a trigger on each table holding a rule's fields, which keeps its
records' rows with the functions here. ``btree_gist`` lets one exclusion constraint
compare a project, a rule and what ranges are per with ``=``, and ranges with ``&&``.
"""

import django.contrib.postgres.constraints
import django.contrib.postgres.fields
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
import django.db.models.functions.datetime
from django.conf import settings
from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations, models

import iki_server.core.ids

# What a record's row is under a rule: its fields' values, as a JSON array of text, or
# NULL when one is empty or the record doesn't count (`while`). `r` is the table's row as
# JSON; `columns` a JSON array of the columns compared; `counted` a JSON object of each
# column a record counts by, to the texts it counts in.
VALUES = r"""
CREATE OR REPLACE FUNCTION iki_rule_values(r jsonb, columns text, counted text) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
DECLARE
    c text;
    allowed jsonb;
    v text;
    out jsonb := '[]'::jsonb;
BEGIN
    FOR c, allowed IN SELECT key, value FROM jsonb_each(counted::jsonb) LOOP
        v := r ->> c;
        IF v IS NULL OR NOT (allowed ? v) THEN
            RETURN NULL;
        END IF;
    END LOOP;
    FOR c IN SELECT jsonb_array_elements_text(columns::jsonb) LOOP
        v := r ->> c;
        IF v IS NULL THEN
            RETURN NULL;
        END IF;
        out := out || jsonb_build_array(v);
    END LOOP;
    RETURN out::text;
END
$$;

-- A record's range, from one column to another: dates from midnight UTC on the first to
-- midnight after the last, datetimes and numbers from where it starts up to where it
-- ends. Nothing when either is empty, or it ends before it starts.
CREATE OR REPLACE FUNCTION iki_rule_range(
    r jsonb, low text, high text, kind text, OUT during tstzrange, OUT span numrange
)
LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE
    a text := r ->> low;
    b text := r ->> high;
BEGIN
    IF a IS NULL OR b IS NULL THEN
        RETURN;
    END IF;
    IF kind = 'date' THEN
        IF a::date > b::date THEN
            RETURN;
        END IF;
        during := tstzrange(a::date::timestamp AT TIME ZONE 'UTC',
                            (b::date + 1)::timestamp AT TIME ZONE 'UTC', '[)');
    ELSIF kind = 'datetime' THEN
        IF a::timestamptz > b::timestamptz THEN
            RETURN;
        END IF;
        during := tstzrange(a::timestamptz, b::timestamptz, '[)');
    ELSE
        IF a::numeric > b::numeric THEN
            RETURN;
        END IF;
        span := numrange(a::numeric, b::numeric, '[)');
    END IF;
END
$$;

-- TG_ARGV: the rule's key (`table:rule`), its columns, and what counts, as above.
CREATE OR REPLACE FUNCTION iki_rule_unique_keep() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    new_value text;
    old_value text;
BEGIN
    EXECUTE format('SELECT %I.iki_rule_values($1, $2, $3)', TG_TABLE_SCHEMA)
        INTO new_value USING to_jsonb(NEW), TG_ARGV[1], TG_ARGV[2];
    IF TG_OP = 'UPDATE' THEN
        EXECUTE format('SELECT %I.iki_rule_values($1, $2, $3)', TG_TABLE_SCHEMA)
            INTO old_value USING to_jsonb(OLD), TG_ARGV[1], TG_ARGV[2];
        IF new_value IS NOT DISTINCT FROM old_value THEN
            RETURN NULL;
        END IF;
    END IF;
    EXECUTE format('DELETE FROM %I.iki_unique_value WHERE entity_id = $1 AND key = $2',
                   TG_TABLE_SCHEMA)
        USING NEW.entity_id, TG_ARGV[0];
    IF new_value IS NOT NULL THEN
        EXECUTE format(
            'INSERT INTO %1$I.iki_unique_value (project_id, key, value, entity_id) '
            'SELECT owner_project_id, $2, $3, id FROM %1$I.iki_entity '
            'WHERE id = $1 AND owner_project_id IS NOT NULL',
            TG_TABLE_SCHEMA)
            USING NEW.entity_id, TG_ARGV[0], new_value;
    END IF;
    RETURN NULL;
END
$$;

-- TG_ARGV: the rule's key, the columns ranges are per, what counts, the columns the range
-- starts and ends at, and the kind of range (date, datetime or number).
CREATE OR REPLACE FUNCTION iki_rule_range_keep() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    new_scope text;
    old_scope text;
    new_during tstzrange;
    old_during tstzrange;
    new_span numrange;
    old_span numrange;
BEGIN
    EXECUTE format('SELECT %I.iki_rule_values($1, $2, $3)', TG_TABLE_SCHEMA)
        INTO new_scope USING to_jsonb(NEW), TG_ARGV[1], TG_ARGV[2];
    EXECUTE format('SELECT during, span FROM %I.iki_rule_range($1, $2, $3, $4)', TG_TABLE_SCHEMA)
        INTO new_during, new_span USING to_jsonb(NEW), TG_ARGV[3], TG_ARGV[4], TG_ARGV[5];
    IF TG_OP = 'UPDATE' THEN
        EXECUTE format('SELECT %I.iki_rule_values($1, $2, $3)', TG_TABLE_SCHEMA)
            INTO old_scope USING to_jsonb(OLD), TG_ARGV[1], TG_ARGV[2];
        EXECUTE format('SELECT during, span FROM %I.iki_rule_range($1, $2, $3, $4)',
                       TG_TABLE_SCHEMA)
            INTO old_during, old_span USING to_jsonb(OLD), TG_ARGV[3], TG_ARGV[4], TG_ARGV[5];
        IF new_scope IS NOT DISTINCT FROM old_scope AND new_during IS NOT DISTINCT FROM old_during
           AND new_span IS NOT DISTINCT FROM old_span THEN
            RETURN NULL;
        END IF;
    END IF;
    EXECUTE format('DELETE FROM %I.iki_range_value WHERE entity_id = $1 AND key = $2',
                   TG_TABLE_SCHEMA)
        USING NEW.entity_id, TG_ARGV[0];
    IF new_scope IS NOT NULL AND (new_during IS NOT NULL OR new_span IS NOT NULL) THEN
        EXECUTE format(
            'INSERT INTO %1$I.iki_range_value (project_id, key, scope, during, span, entity_id) '
            'SELECT owner_project_id, $2, $3, $4, $5, id FROM %1$I.iki_entity '
            'WHERE id = $1 AND owner_project_id IS NOT NULL',
            TG_TABLE_SCHEMA)
            USING NEW.entity_id, TG_ARGV[0], new_scope, new_during, new_span;
    END IF;
    RETURN NULL;
END
$$;
"""

DROP = """
DROP FUNCTION IF EXISTS iki_rule_range_keep();
DROP FUNCTION IF EXISTS iki_rule_unique_keep();
DROP FUNCTION IF EXISTS iki_rule_range(jsonb, text, text, text);
DROP FUNCTION IF EXISTS iki_rule_values(jsonb, text, text);
"""


class Migration(migrations.Migration):
    dependencies = [
        ("iki_audit", "0005_via_trigger"),
        ("iki_entities", "0013_signatures"),
        ("iki_org", "0005_lab_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # A trusted extension: the database's owner (the migrator) may create it.
        BtreeGistExtension(),
        migrations.RunSQL(VALUES, DROP),
        migrations.CreateModel(
            name="RulesChecked",
            fields=[
                ("type", models.CharField(max_length=160, primary_key=True, serialize=False)),
                ("digest", models.CharField(max_length=64)),
                (
                    "checked_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
            ],
            options={
                "db_table": "iki_rules_checked",
                "db_table_comment": "What each type's records were last checked against (ADR 0127).",
            },
        ),
        migrations.CreateModel(
            name="RangeValue",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("key", models.CharField(max_length=127)),
                ("scope", models.CharField(max_length=600)),
                (
                    "during",
                    django.contrib.postgres.fields.ranges.DateTimeRangeField(blank=True, null=True),
                ),
                (
                    "span",
                    django.contrib.postgres.fields.ranges.DecimalRangeField(blank=True, null=True),
                ),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="iki_entities.entity",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_org.project",
                    ),
                ),
            ],
            options={
                "db_table": "iki_range_value",
                "db_table_comment": "Each record's range under a rule that ranges mustn't overlap (ADR 0127). Kept by triggers on the plugin tables.",
                "constraints": [
                    django.contrib.postgres.constraints.ExclusionConstraint(
                        condition=models.Q(("during__isnull", False)),
                        expressions=[
                            ("project", "="),
                            ("key", "="),
                            ("scope", "="),
                            ("during", "&&"),
                        ],
                        name="iki_range_value_apart",
                    ),
                    django.contrib.postgres.constraints.ExclusionConstraint(
                        condition=models.Q(("span__isnull", False)),
                        expressions=[
                            ("project", "="),
                            ("key", "="),
                            ("scope", "="),
                            ("span", "&&"),
                        ],
                        name="iki_range_value_span_apart",
                    ),
                    models.UniqueConstraint(
                        fields=("entity", "key"), name="iki_range_value_entity"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("during__isnull", False), ("span__isnull", False), _connector="OR"
                        ),
                        name="iki_range_value_range",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RuleBreach",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("rule", models.CharField(max_length=160)),
                ("label", models.CharField(max_length=100)),
                ("message", models.TextField()),
                (
                    "found_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="breaches",
                        to="iki_entities.entity",
                    ),
                ),
            ],
            options={
                "db_table": "iki_rule_breach",
                "db_table_comment": "Records that break a rule added or changed after they were saved.",
                "constraints": [
                    models.UniqueConstraint(fields=("entity", "rule"), name="iki_rule_breach_once")
                ],
            },
        ),
        migrations.CreateModel(
            name="RuleHold",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=iki_server.core.ids.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("rule", models.CharField(max_length=160)),
                ("label", models.CharField(max_length=100)),
                ("message", models.TextField()),
                (
                    "roles",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=112)
                    ),
                ),
                ("revision", models.PositiveIntegerField()),
                ("last_revision", models.PositiveIntegerField()),
                (
                    "held_at",
                    models.DateTimeField(db_default=django.db.models.functions.datetime.Now()),
                ),
                ("reason", models.TextField()),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_reason", models.TextField(blank=True, default="")),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="holds",
                        to="iki_entities.entity",
                    ),
                ),
                (
                    "held_agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="iki_audit.agent",
                    ),
                ),
                (
                    "held_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "iki_rule_hold",
                "db_table_comment": "Records held by rules their saves broke (ADR 0127), and who reviewed each, when and why.",
                "indexes": [
                    models.Index(
                        condition=models.Q(("reviewed_at__isnull", True)),
                        fields=["entity"],
                        name="iki_rule_hold_held",
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("reviewed_at__isnull", True)),
                        fields=("entity", "rule"),
                        name="iki_rule_hold_open",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("reviewed_at__isnull", True), ("reviewed_by__isnull", True)),
                            models.Q(
                                ("reviewed_at__isnull", False), ("reviewed_by__isnull", False)
                            ),
                            _connector="OR",
                        ),
                        name="iki_rule_hold_review",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("reason", ""), _negated=True),
                        name="iki_rule_hold_reason",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("reviewed_at__isnull", True),
                            models.Q(("review_reason", ""), _negated=True),
                            _connector="OR",
                        ),
                        name="iki_rule_hold_review_reason",
                    ),
                ],
            },
        ),
    ]
