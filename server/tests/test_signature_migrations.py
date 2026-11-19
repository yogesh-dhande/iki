"""Electronic signatures across what Iki supports doing to a record after it's signed
(ADR 0148): a plugin's migration renaming a field it signed, converting a quantity it
signed to another unit, or both and back; and its project moving it to the registry entry
that replaced its own (ADR 0115). Each still verifies, and a value changed in SQL, before
or after, still fails its record check.

The plugins are built and migrated on a database of their own, in subprocesses, as
``test_plugin_migrations`` does, since their apps can't be installed in this process.
"""

import json
import textwrap
from pathlib import Path
from typing import Any

import psycopg
import pytest
from test_plugin_migrations import build, database, django, made, orm, query, sync
from test_plugin_migrations import migrate as migrate_only

__all__ = ["database"]  # a fixture, from test_plugin_migrations

LAB = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
roles:
  analyst: { label: Analyst }
"""

#: Signed for once: its analyst signs it into review.
SIGNED = """\
  status:
    type: state
    initial: received
    states:
      received: { label: Received, set_by: [analyst] }
      in_review:
        label: In review
        set_by: [analyst]
        signatures: [{ meaning: authorship }]
"""

SAMPLE = (
    """\
apiVersion: iki/v1
kind: EntityType
name: Sample
schema_version: 1
hrid: { prefix: SMP }
fields:
  name: { type: string, max_length: 100 }
  mass: { type: quantity, unit: g, min: 0 }
  temperature: { type: quantity, unit: Cel }
"""
    + SIGNED
)

#: A reagent registered by its code (ADR 0115), with a fact its registry keeps: its CAS
#: number (sodium chloride's).
REAGENT = (
    """\
apiVersion: iki/v1
kind: EntityType
name: Reagent
schema_version: 1
hrid: { prefix: RGT }
identity: [code]
fields:
  code: { type: string, required: true, immutable: true, max_length: 20 }
  cas: { type: string, registry: fact, pattern: "^[1-9][0-9]{1,6}-[0-9]{2}-[0-9]$" }
  note: { type: string, max_length: 100 }
"""
    + SIGNED
)

#: People to sign, an identity provider they sign in again at, and the organization's
#: certification (21 CFR 11.100(c)); and how they sign, as if they'd signed in again.
PEOPLE = """
import datetime as dt
from allauth.socialaccount.models import SocialAccount
from django.utils import timezone
from iki_server.accounts.models import IdentityProvider, User
from iki_server.entities import entries
from iki_server.entities import services as entities
from iki_server.entities.models import Entity
from iki_server.org.models import (
    LabRoleGrant, OrgRole, OrgRoleAssignment, ProjectMembership, ProjectRole, Registrar,
)
from iki_server.signatures import services as signatures

provider = IdentityProvider(
    display_name="Test SSO", issuer="https://idp.example.com", client_id="iki-test",
    authorization_endpoint="https://idp.example.com/authorize", is_enabled=True,
    enabled_at=timezone.now(),
)
provider.client_secret = "secret"
provider.save()


def person(name, *projects):
    user = User.objects.create_user(f"{name}@example.com", name.title())
    SocialAccount.objects.create(user=user, provider=provider.provider_id, uid=f"uid-{name}")
    for p in projects:
        membership = ProjectMembership.objects.create(
            project=p, user=user, role=ProjectRole.EDITOR
        )
        LabRoleGrant.objects.create(membership=membership, role="acme/lab.analyst")
    return user


admin = person("admin")
OrgRoleAssignment.objects.create(user=admin, role=OrgRole.ORG_ADMIN)
signatures.certify(admin, dt.date(2026, 11, 1), "QA-DOC-0042")


def sign(user, record):
    record = Entity.objects.get(pk=record.pk)
    signing = signatures.begin(
        user, f"session-{user.pk}", record, "status", "in_review", index=None,
        expected_revision=record.revision, reason="", return_to=f"/entities/{record.hrid}",
        acknowledge=True,
    )
    evidence = signatures.Evidence(
        protocol="oidc", issuer=provider.issuer, subject=f"uid-{user.email.split('@')[0]}",
        authenticated_at=timezone.now(), methods=("pwd", "otp"),
    )
    done = signatures.finish(signing, evidence)
    assert done.status == "signed", done.message
"""

#: Each signature's record check, by its record's HRID: whether it passed, and what it says.
VERIFIED = """
from iki_server.signatures import services as signatures
from iki_server.signatures.models import Signature
result = {}
for s in Signature.objects.select_related("entity"):
    checks = {c.name: c for c in signatures.verify(s)}
    assert all(c.ok for name, c in checks.items() if name != "record"), checks
    result[s.entity.hrid] = [checks["record"].ok, checks["record"].detail]
"""


def migrate(out: Path, database: str, *args: str) -> None:
    """Migrates, and syncs what migrations don't do, as a deploy's migrate Job does."""
    migrate_only(out, database, *args)
    sync(out, database)


def verified(out: Path, database: str) -> dict[str, tuple[bool, str]]:
    """Each signature's record check, verified as Iki verifies it, by ``out``'s code."""
    script = "import django, json\ndjango.setup()\n" + textwrap.dedent(VERIFIED)
    script += "\nprint(json.dumps(result))\n"
    ran = django(out, database, "-c", script)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    found = json.loads(ran.stdout.strip().splitlines()[-1])
    return {hrid: (ok, detail) for hrid, (ok, detail) in found.items()}


def tamper(database: str, statement: str, *params: object) -> None:
    """As a database superuser past the append-only triggers: what nothing in Iki can do."""
    with psycopg.connect(database) as connection:
        connection.execute("SET session_replication_role = replica")
        assert connection.execute(statement, params).rowcount == 1


@pytest.fixture
def lab(tmp_path: Path) -> Path:
    root = tmp_path / "plugins" / "lab"
    (root / "types").mkdir(parents=True)
    (root / "plugin.yaml").write_text(LAB)
    return root


#: The sample, its `name` renamed `title`, its mass in mg and its temperature in K.
CHANGED = (
    SAMPLE.replace("  name: {", "  title: {")
    .replace("unit: g,", "unit: mg,")
    .replace("unit: Cel }", "unit: K }")
)

HISTORY = "SELECT {} FROM acme__lab_sample__history h JOIN iki_entity e ON e.id = h.entity_id"
HISTORY += " WHERE h.revision = 1 ORDER BY e.hrid"


def test_a_signature_survives_migrations_that_rename_and_convert_what_it_signed(
    lab: Path, database: str
) -> None:
    (lab / "types" / "sample.yaml").write_text(SAMPLE)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        PEOPLE
        + """
analyst = person("analyst", project)
for name, mass, temperature in (("S1", 1.1, 36.6), ("S2", 2.5, 4.0)):
    made = entities.create(analyst, "acme/lab.Sample", project, {
        "name": name,
        "mass": {"value": mass, "unit": "g"},
        "temperature": {"value": temperature, "unit": "Cel"},
    }).entity
    sign(analyst, made)
result = None
""",
    )
    good = "SMP-000001’s revision 1 still holds what was signed."
    assert verified(out, database) == {
        "SMP-000001": (True, good),
        "SMP-000002": (True, good.replace("1’s", "2’s")),
    }

    # Changed in SQL, before any migration: caught.
    tamper(
        database,
        "UPDATE acme__lab_sample__history h SET mass = 2.6 FROM iki_entity e "
        "WHERE e.id = h.entity_id AND e.hrid = 'SMP-000002' AND h.revision = 1",
    )
    changed = (
        False,
        "SMP-000002’s revision 1 no longer holds what was signed: `mass` changed. Its history "
        "was changed outside Iki.",
    )
    assert verified(out, database)["SMP-000002"] == changed

    # The plugin renames `name` to `title`, and keeps mass in mg and temperatures in K.
    (lab / "types" / "sample.yaml").write_text(CHANGED)
    made("lab", "--name", "changes", cwd=lab.parent, answers="y\n")
    migration = (lab / "migrations" / "0002_changes.py").read_text()
    assert "RecordRename(" in migration and "part='acme/lab.Sample'" in migration
    assert ("iki_plugins", "0006_history_changes") in _dependencies(migration)
    out = build(lab.parent, "lab")
    migrate(out, database)
    # What the migration did is kept, and nothing changes or deletes it, its owner included.
    assert query(
        database, 'SELECT plugin, kind, part, name, "to" FROM iki_history_change ORDER BY id'
    ) == [
        ("acme/lab", "rename", "acme/lab.Sample", "name", "title"),
        ("acme/lab", "convert", "acme/lab.Sample", "mass", "mass"),
        ("acme/lab", "convert", "acme/lab.Sample", "temperature", "temperature"),
    ]
    for statement in ("UPDATE iki_history_change SET scale = 1", "DELETE FROM iki_history_change"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            query(database, statement)
    # Its history holds the same quantities in their new units, as doubles: 1.1 g is
    # 1100.0000000000002 mg.
    assert query(database, HISTORY.format("h.title, h.mass, h.temperature")) == [
        ("S1", 1.1 * 1000, 36.6 + 273.15),
        ("S2", 2.6 * 1000, 4.0 + 273.15),
    ]
    since = (
        "as plugins’ migrations have kept it since: `name` renamed `title`, `mass` converted "
        "from g to mg, `temperature` converted from Cel to K."
    )
    after = verified(out, database)
    assert after["SMP-000001"] == (
        True,
        f"SMP-000001’s revision 1 still holds what was signed, {since}",
    )
    assert after["SMP-000002"] == changed

    # Rolled back, and served by the build before (ADR 0034), it's read through that too.
    migrate_only(out, database, "acme__lab", "0001")
    assert query(database, HISTORY.format("h.name, h.mass")) == [("S1", 1.1), ("S2", 2.6)]
    before = build_at(lab, "0001")
    sync(before, database)
    back = verified(before, database)
    assert back["SMP-000001"][0] is True, back
    for undone in ("`mass` converted from mg to g", "`title` renamed `name`"):
        assert undone in back["SMP-000001"][1]
    assert back["SMP-000002"] == changed

    # And forwards again; then changed in SQL after the migration: caught.
    out = build(lab.parent, "lab")
    migrate(out, database)
    assert verified(out, database)["SMP-000001"][0] is True
    tamper(
        database,
        "UPDATE acme__lab_sample__history h SET mass = mass + 1 FROM iki_entity e "
        "WHERE e.id = h.entity_id AND e.hrid = 'SMP-000001' AND h.revision = 1",
    )
    assert verified(out, database)["SMP-000001"] == (
        False,
        "SMP-000001’s revision 1 no longer holds what was signed: `mass` changed. Its history "
        "was changed outside Iki.",
    )


def build_at(lab: Path, last: str) -> Path:
    """The plugin as it was at its migration ``last``, built: the build a rollback deploys.
    The plugin's own files are as they are now again after."""
    later = [m for m in (lab / "migrations").glob("0*.py") if m.name.split("_")[0] > last]
    now = (lab / "types" / "sample.yaml").read_text()
    for migration in later:
        migration.rename(migration.with_suffix(".later"))
    try:
        (lab / "types" / "sample.yaml").write_text(SAMPLE)
        return build(lab.parent, "lab")
    finally:
        (lab / "types" / "sample.yaml").write_text(now)
        for migration in later:
            migration.with_suffix(".later").rename(migration)


def _dependencies(migration: str) -> list[tuple[str, str]]:
    import ast

    tree = ast.parse(migration)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "dependencies":
            found: list[tuple[str, str]] = ast.literal_eval(node.value)
            return found
    return []


def test_a_signature_survives_moving_its_record_to_the_replacement_entry(
    lab: Path, database: str
) -> None:
    (lab / "types" / "reagent.yaml").write_text(REAGENT)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    result: dict[str, Any] = orm(
        out,
        database,
        PEOPLE
        + """
from iki_server.org.models import Project
screens = Project.objects.create(team=team, name="Screens")
analyst = person("analyst", project, screens)
curator = person("curator")
Registrar.objects.create(registry=project.registry, user=curator)
# Screens registers a reagent by a mistyped code, and signs its record into review.
wrong = entities.create(
    analyst, "acme/lab.Reagent", screens,
    {"code": "NACL-0l", "cas": "7647-14-5", "note": "Opened 2026-11-02"},
).entity
right = entities.create(
    analyst, "acme/lab.Reagent", project, {"code": "NACL-01", "cas": "7647-14-5"}
).entity
sign(analyst, wrong)
wrong_entry = Entity.objects.get(pk=wrong.entry_id)
from iki_server.signatures.models import Signature
before = {c.name: c.ok for c in signatures.verify(Signature.objects.get(entity_id=wrong.pk))}
# A registrar supersedes the mistyped entry; Screens moves its record onto the right one.
entries.supersede(curator, wrong_entry, Entity.objects.get(pk=right.entry_id), reason="Typo")
entries.move(analyst, Entity.objects.get(pk=wrong.pk), reason="Superseded")
moved = Entity.objects.get(pk=wrong.pk)
result = {
    "before": before,
    "moved_to": [str(moved.entry_id), str(right.entry_id)],
    "old_entry": wrong_entry.hrid,
    "record": moved.hrid,
}
""",
    )
    assert all(result["before"].values()), result["before"]
    assert result["moved_to"][0] == result["moved_to"][1]
    hrid = result["record"]
    # Its earlier signature still verifies, against the entry it was of when signed.
    assert verified(out, database) == {
        hrid: (True, f"{hrid}’s revision 1 still holds what was signed.")
    }

    # The old entry's history, changed in SQL, is caught.
    tamper(
        database,
        "UPDATE acme__lab_reagent__entry__history h SET cas = '7758-02-3' FROM iki_entity e "
        "WHERE e.id = h.entity_id AND e.hrid = %s AND h.revision = 1",
        result["old_entry"],
    )
    assert verified(out, database) == {
        hrid: (
            False,
            f"{hrid}’s revision 1 no longer holds what was signed: `cas` changed. Its "
            "history was changed outside Iki.",
        )
    }


def test_a_signature_survives_its_type_renamed(lab: Path, database: str) -> None:
    (lab / "types" / "sample.yaml").write_text(SAMPLE)
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        PEOPLE
        + """
analyst = person("analyst", project)
sign(analyst, entities.create(analyst, "acme/lab.Sample", project, {"name": "S1"}).entity)
result = None
""",
    )
    (lab / "types" / "sample.yaml").write_text(SAMPLE.replace("name: Sample", "name: Specimen"))
    made("lab", "--name", "specimen", cwd=lab.parent, answers="y\n")
    out = build(lab.parent, "lab")
    migrate(out, database)
    assert query(database, "SELECT type FROM iki_entity WHERE hrid = 'SMP-000001'") == [
        ("acme/lab.Specimen",)
    ]
    assert verified(out, database) == {
        "SMP-000001": (
            True,
            "SMP-000001’s revision 1 still holds what was signed, as plugins’ migrations have "
            "kept it since: its type renamed acme/lab.Specimen.",
        )
    }
    # Each change recorded is signed by the deployment's key, as a signature is.
    [(key,)] = query(database, "SELECT key FROM iki_history_change")
    assert key == query(database, "SELECT public FROM iki_signing_key")[0][0]

    # A value changed in SQL, with a change to history written beside it to explain it:
    # the database alone can't sign one, so it explains nothing.
    tamper(
        database,
        "UPDATE acme__lab_specimen__history SET name = 'S2' WHERE revision = 1",
    )
    assert verified(out, database)["SMP-000001"] == (
        False,
        "SMP-000001’s revision 1 no longer holds what was signed: `name` changed. Its history "
        "was changed outside Iki.",
    )
    query(
        database,
        'INSERT INTO iki_history_change (plugin, backwards, kind, part, name, "to",'
        " from_unit, to_unit, made_at, key, signature)"
        " SELECT plugin, false, 'rename', 'acme/lab.Specimen', 'title', 'name', '', '', now(), key,"
        " signature"
        " FROM iki_history_change",
    )
    assert verified(out, database)["SMP-000001"] == (
        False,
        "SMP-000001’s revision 1 no longer holds what was signed: a change to its history "
        "recorded since (rename of `title`) isn’t signed by this deployment’s key, as a "
        "migration’s is. Its history was changed outside Iki.",
    )
