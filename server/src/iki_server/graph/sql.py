"""The SQL that keeps the graph's edges (ADR 0123), shared by the migration that makes its
triggers and ``sync_plugins``, which installs them on plugins' tables and fills them in.

Each statement is written once, for a trigger's transition tables (``added`` and
``removed``) and the schema it runs in (``%1$I``, from ``TG_TABLE_SCHEMA``):
:func:`backfill` turns one into a statement over a table already there.
"""

# ruff: noqa: S608 - SQL of our own, with no values in it

from __future__ import annotations

#: Edges of runs' inputs, from ``iki_lineage``'s new rows: what each entity a run made was
#: made from, and what those inputs' references included (ADR 0105).
LINEAGE = r"""
INSERT INTO %1$I.iki_graph_edge (node_id, on_id, kind, field, run_id, revision)
SELECT l.entity_id, l.parent_id, 'run', l.input, l.run_id, l.parent_revision
FROM added l WHERE l.entity_id <> l.parent_id
UNION ALL
SELECT l.entity_id, inc.entity_id, 'run', l.input || '.' || inc.field, l.run_id, inc.revision
FROM added l
JOIN %1$I.iki_run_input ri
  ON ri.run_id = l.run_id AND ri.name = l.input AND ri.entity_id = l.parent_id
JOIN %1$I.iki_run_inclusion inc
  ON inc.run_id = l.run_id AND inc.input = ri.name AND inc.position = ri.position
WHERE inc.entity_id <> l.entity_id
"""

#: Edges of what runs wrote, from ``iki_run_effect``'s new rows: an entity a run updated
#: depends on the run's other inputs, and anything it wrote depends on what held the files
#: it was given as params, or on what the run that wrote such a file read.
EFFECTS = r"""
INSERT INTO %1$I.iki_graph_edge (node_id, on_id, kind, field, run_id, file_id, revision)
WITH runs AS (
  SELECT DISTINCT r.id, r.spec, r.params
  FROM %1$I.iki_action_run r WHERE r.id IN (SELECT run_id FROM added)
), given AS (
  SELECT DISTINCT r.id AS run_id, p.key AS param, v.file_id
  FROM runs r
  CROSS JOIN LATERAL jsonb_each(
    CASE WHEN jsonb_typeof(r.spec->'params') = 'object' THEN r.spec->'params'
    ELSE '{}'::jsonb END) p
  CROSS JOIN LATERAL (
    SELECT r.params->>p.key AS text
    WHERE p.value->>'type' = 'file' AND jsonb_typeof(r.params->p.key) = 'string'
    UNION ALL
    SELECT x FROM jsonb_array_elements_text(
      CASE WHEN p.value->>'type' = 'list' AND p.value->'items'->>'type' = 'file'
        AND jsonb_typeof(r.params->p.key) = 'array' THEN r.params->p.key
      ELSE '[]'::jsonb END) x
  ) t
  CROSS JOIN LATERAL (
    SELECT t.text::uuid AS file_id
    WHERE t.text ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
  ) v
), made AS (
  SELECT DISTINCT entity_id, run_id, kind FROM added
)
SELECT m.entity_id, ri.entity_id, 'run', ri.name, m.run_id, NULL::uuid, ri.revision
FROM made m JOIN %1$I.iki_run_input ri ON ri.run_id = m.run_id
WHERE m.kind = 'updated' AND ri.entity_id <> m.entity_id
UNION ALL
SELECT m.entity_id, u.entity_id, 'file', g.param, m.run_id, g.file_id, NULL::integer
FROM made m JOIN given g ON g.run_id = m.run_id
JOIN %1$I.iki_file_use u ON u.file_id = g.file_id
WHERE u.entity_id <> m.entity_id
UNION ALL
SELECT DISTINCT m.entity_id, ri.entity_id, 'file', g.param, m.run_id, g.file_id, ri.revision
FROM made m JOIN given g ON g.run_id = m.run_id
JOIN %1$I.iki_upload up ON up.file_id = g.file_id
JOIN (
  SELECT upload_id, run_id FROM %1$I.iki_run_upload
  UNION ALL SELECT upload_id, run_id FROM %1$I.iki_run_artifact
) wrote ON wrote.upload_id = up.id
JOIN %1$I.iki_run_input ri ON ri.run_id = wrote.run_id
WHERE ri.entity_id <> m.entity_id
"""

#: Edges between new records and their registry entries, both ways (ADR 0115).
RECORDS = r"""
INSERT INTO %1$I.iki_graph_edge (node_id, on_id, kind)
SELECT e.id, e.entry_id, 'record' FROM added e WHERE e.entry_id IS NOT NULL
UNION ALL
SELECT e.entry_id, e.id, 'record' FROM added e WHERE e.entry_id IS NOT NULL
"""


def backfill(statement: str, table: str) -> str:
    """``statement`` over everything already in ``table``, in the current schema."""
    return statement.replace("%1$I.", "").replace("FROM added", f"FROM {table}")


def _function(name: str, body: str) -> str:
    return f"""
CREATE FUNCTION {name}() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
{body}
    RETURN NULL;
END
$$;
"""


def _execute(statement: str) -> str:
    return f"    EXECUTE format($q${statement}$q$, TG_TABLE_SCHEMA);"


#: Statement triggers: a bulk import's millions of rows make their edges in one statement.
CORE = (
    _function("iki_graph_lineage", _execute(LINEAGE))
    + _function("iki_graph_effects", _execute(EFFECTS))
    + _function("iki_graph_records", _execute(RECORDS))
    + r"""
CREATE FUNCTION iki_graph_moved() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    EXECUTE format(
        'DELETE FROM %1$I.iki_graph_edge WHERE kind = ''record'' AND (node_id = $1 OR on_id = $1)',
        TG_TABLE_SCHEMA) USING NEW.id;
    IF NEW.entry_id IS NOT NULL THEN
        EXECUTE format(
            'INSERT INTO %1$I.iki_graph_edge (node_id, on_id, kind) '
            'VALUES ($1, $2, ''record''), ($2, $1, ''record'')',
            TG_TABLE_SCHEMA) USING NEW.id, NEW.entry_id;
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER iki_graph_lineage AFTER INSERT ON iki_lineage
REFERENCING NEW TABLE AS added FOR EACH STATEMENT EXECUTE FUNCTION iki_graph_lineage();
CREATE TRIGGER iki_graph_effects AFTER INSERT ON iki_run_effect
REFERENCING NEW TABLE AS added FOR EACH STATEMENT EXECUTE FUNCTION iki_graph_effects();
CREATE TRIGGER iki_graph_records AFTER INSERT ON iki_entity
REFERENCING NEW TABLE AS added FOR EACH STATEMENT EXECUTE FUNCTION iki_graph_records();
CREATE TRIGGER iki_graph_moved AFTER UPDATE OF entry_id ON iki_entity
FOR EACH ROW WHEN (OLD.entry_id IS DISTINCT FROM NEW.entry_id)
EXECUTE FUNCTION iki_graph_moved();
"""
)

#: A plugin table's references, as ``sync_plugins`` installs their triggers: its arguments
#: are pairs of a column and the field it holds. Each statement takes away the edges of the
#: values it changed and adds those of their new ones.
REFS = r"""
CREATE FUNCTION iki_graph_refs() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    i integer := 0;
    col text;
    src text;
BEGIN
    WHILE i < TG_NARGS LOOP
        col := TG_ARGV[i];
        src := TG_TABLE_NAME || '.' || col;
        IF TG_OP = 'DELETE' THEN
            EXECUTE format(
                'DELETE FROM %1$I.iki_graph_edge g USING removed o '
                'WHERE g.kind = ''reference'' AND g.source = $1 AND g.node_id = o.entity_id',
                TG_TABLE_SCHEMA) USING src;
        ELSIF TG_OP = 'UPDATE' THEN
            EXECUTE format(
                'DELETE FROM %1$I.iki_graph_edge g USING removed o '
                'JOIN added n ON n.entity_id = o.entity_id '
                'WHERE g.kind = ''reference'' AND g.source = $1 AND g.node_id = o.entity_id '
                'AND o.%2$I IS DISTINCT FROM n.%2$I',
                TG_TABLE_SCHEMA, col) USING src;
            EXECUTE format(
                'INSERT INTO %1$I.iki_graph_edge (node_id, on_id, kind, field, source) '
                'SELECT n.entity_id, n.%2$I, ''reference'', $2, $1 FROM added n '
                'JOIN removed o ON o.entity_id = n.entity_id '
                'WHERE n.%2$I IS NOT NULL AND n.%2$I <> n.entity_id '
                'AND o.%2$I IS DISTINCT FROM n.%2$I',
                TG_TABLE_SCHEMA, col) USING src, TG_ARGV[i + 1];
        ELSE
            EXECUTE format(
                'INSERT INTO %1$I.iki_graph_edge (node_id, on_id, kind, field, source) '
                'SELECT n.entity_id, n.%2$I, ''reference'', $2, $1 FROM added n '
                'WHERE n.%2$I IS NOT NULL AND n.%2$I <> n.entity_id',
                TG_TABLE_SCHEMA, col) USING src, TG_ARGV[i + 1];
        END IF;
        i := i + 2;
    END LOOP;
    RETURN NULL;
END
$$;

CREATE FUNCTION iki_graph_items() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP IN ('DELETE', 'UPDATE') THEN
        EXECUTE format(
            'DELETE FROM %1$I.iki_graph_edge g USING removed o '
            'WHERE g.kind = ''reference'' AND g.source = $1 AND g.node_id = o.entity_id '
            'AND g.position = o.position',
            TG_TABLE_SCHEMA) USING TG_TABLE_NAME;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        EXECUTE format(
            'INSERT INTO %1$I.iki_graph_edge (node_id, on_id, kind, field, source, position) '
            'SELECT n.entity_id, n.item_id, ''reference'', $2, $1, n.position FROM added n '
            'WHERE n.item_id <> n.entity_id',
            TG_TABLE_SCHEMA) USING TG_TABLE_NAME, TG_ARGV[0];
    END IF;
    RETURN NULL;
END
$$;
"""

DROP_CORE = """
DROP TRIGGER IF EXISTS iki_graph_moved ON iki_entity;
DROP TRIGGER IF EXISTS iki_graph_records ON iki_entity;
DROP TRIGGER IF EXISTS iki_graph_effects ON iki_run_effect;
DROP TRIGGER IF EXISTS iki_graph_lineage ON iki_lineage;
DROP FUNCTION IF EXISTS iki_graph_moved();
DROP FUNCTION IF EXISTS iki_graph_records();
DROP FUNCTION IF EXISTS iki_graph_effects();
DROP FUNCTION IF EXISTS iki_graph_lineage();
"""

DROP_REFS = """
DROP FUNCTION IF EXISTS iki_graph_refs() CASCADE;
DROP FUNCTION IF EXISTS iki_graph_items() CASCADE;
"""
