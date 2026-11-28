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
from test_plugin_migrations import (
    build,
    build_to,
    database,
    deployed,
    django,
    made,
    orm,
    private_database,
    query,
    sync,
)
from test_plugin_migrations import migrate as migrate_only

__all__ = ["database"]  # a fixture, from test_plugin_migrations

LAB = """\
apiVersion: iki/v1
kind: Plugin
name: acme/lab
version: 1.0.0
iki: ">=0.1"
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
  reference: { type: ref, to: Reagent, registry: fact }
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
    LabRoleGrant, OrgRole, OrgRoleAssignment, ProjectMembership, ProjectRole, RegistryRole,
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
        expected_revision=record.revision,
        expected_entry_revision=entities.entry_revision(record),
        reason="", return_to=f"/entities/{record.hrid}",
        acknowledge=True,
    )
    done = reauthenticate(user, signing)
    assert done.status == "signed", done.message


def reauthenticate(user, signing):
    evidence = signatures.Evidence(
        protocol="oidc", issuer=provider.issuer, subject=f"uid-{user.email.split('@')[0]}",
        authenticated_at=timezone.now(), methods=("pwd", "otp"),
    )
    return signatures.finish(signing, evidence)
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


def test_a_signed_revision_reads_under_the_old_field_name_after_rollback(
    lab: Path, database: str
) -> None:
    manifest = lab / "types" / "sample.yaml"
    manifest.write_text(SAMPLE)
    made("lab", "--no-input", cwd=lab.parent)
    first = build_to(lab.parent, "v1", "lab")
    deployed(first, database)
    manifest.write_text(SAMPLE.replace("  name: {", "  title: {"))
    made("lab", "--name", "rename", cwd=lab.parent, answers="y\n")
    second = build_to(lab.parent, "v2", "lab")
    deployed(second, database)
    orm(
        second,
        database,
        PEOPLE
        + """
analyst = person("analyst", project)
record = entities.create(analyst, "acme/lab.Sample", project, {"title": "S1"}).entity
sign(analyst, record)
record.refresh_from_db()
entities.update(analyst, record, {"title": "S2"}, expected_revision=record.revision)
result = None
""",
    )
    deployed(first, database)
    ok, detail = verified(first, database)["SMP-000001"]
    assert ok, detail
    assert query(database, "SELECT name FROM acme__lab_sample__history ORDER BY revision") == [
        ("S1",),
        ("S1",),
        ("S2",),
    ]


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
        "read through what was done to it since: `name` renamed `title`, `mass` converted "
        "from g to mg (× 1000), `temperature` converted from Cel to K (× 1 + 273.15)."
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
    for undone in ("`mass` converted from mg to g (× 0.001)", "`title` renamed `name`"):
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


def test_a_restored_signed_entry_keeps_its_reversal_after_a_state_field_rename(
    lab: Path,
    database: str,
    _postgres_url: str,
    tmp_path: Path,
    s3: Any,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source renamed status to phase after signing. The receiving lab installed
    the newer schema from the outset and has no local rename operation."""
    monkeypatch.setenv("IKI_HTTPS", "false")
    for key in (
        "IKI_FILES_BUCKET",
        "IKI_FILES_PREFIX",
        "IKI_FILES_ENDPOINT_URL",
        "IKI_FILES_PUBLIC_ENDPOINT_URL",
    ):
        monkeypatch.setenv(key, getattr(settings, key))
    manifest = lab / "types/sample.yaml"
    manifest.write_text(SAMPLE)
    # Signed and exported at another deployment, whose own key signs its signatures.
    monkeypatch.setenv("IKI_ENVIRONMENT", "Lab A")
    made("lab", "--no-input", cwd=lab.parent)
    source = build(lab.parent, "lab")
    migrate(source, database)
    orm(
        source,
        database,
        PEOPLE
        + """
analyst = person("analyst", project)
ProjectMembership.objects.create(project=project, user=admin, role=ProjectRole.ADMIN)
sample = entities.create(analyst, "acme/lab.Sample", project, {"name": "Reviewed sample"}).entity
sign(analyst, sample)
result = None
""",
    )
    newer = SAMPLE.replace("  status:", "  phase:").replace(
        "schema_version: 1", "schema_version: 2"
    )
    manifest.write_text(newer)
    (lab / "plugin.yaml").write_text(LAB.replace("1.0.0", "1.1.0"))
    made("lab", "--name", "rename", cwd=lab.parent, answers="y\n")
    source = build(lab.parent, "lab")
    migrate(source, database)
    archive = tmp_path / "reviewed-sample.tar.gz"
    exported = django(
        source,
        database,
        "-c",
        f"""
import django
django.setup()
from pathlib import Path
from iki_server.accounts.models import User
from iki_server.bundles import export
from iki_server.entities.models import Entity
sample = Entity.objects.get(type="acme/lab.Sample")
export.write(
    sample.owner_project, [sample], Path({str(archive)!r}),
    by=User.objects.get(email="admin@example.com"),
    reason="Hand over signed work", selection="",
)
""",
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    monkeypatch.setenv("IKI_ENVIRONMENT", "test")
    target = tmp_path / "receiving/plugins/lab"
    (target / "types").mkdir(parents=True)
    (target / "plugin.yaml").write_text((lab / "plugin.yaml").read_text())
    (target / "types/sample.yaml").write_text(newer)
    made("lab", "--no-input", cwd=target.parent)
    receiver = build(target.parent, "lab")
    with private_database(_postgres_url) as db:
        migrate(receiver, db)
        restored = django(
            receiver,
            db,
            "-c",
            f"""
import django
django.setup()
from pathlib import Path
from django.conf import settings
from api_client import Api
from org_world import build_world
from test_bundle_imports import PUBLIC, applied, upload
from iki_server.entities.models import Entity, Revision
from iki_server.org.models import OrgRole, OrgRoleAssignment
from iki_server.signatures import changes
world = build_world()
admin = world.users["admin"]
OrgRoleAssignment.objects.create(user=admin, role=OrgRole.ORG_ADMIN)
settings.IKI_BUNDLE_TRUSTED_KEYS = {{"Lab A": (PUBLIC,)}}
api = Api(world.browser("admin"))
done = applied(api, upload(api, world.project, Path({str(archive)!r})))
assert done["state"] == "applied", done
record = Entity.objects.get(type="acme/lab.Sample")
assert Revision.objects.get(entity=record, number=2).state_field == "status"
exit = changes.find(record, "phase", "received")
assert exit.reversal and exit.reason == "required"
assert exit.signatures[0].label == "Reversal of Authorship"
""",
        )
        assert restored.returncode == 0, restored.stdout + restored.stderr


def test_signing_checks_the_registry_entry_revision_the_signer_read(
    lab: Path, database: str
) -> None:
    (lab / "types" / "reagent.yaml").write_text(
        REAGENT
        + """\
      approved:
        label: Approved
        set_by: [analyst]
        signatures: [{ meaning: review }, { meaning: approval }]
"""
    )
    made("lab", "--no-input", cwd=lab.parent)
    out = build(lab.parent, "lab")
    migrate(out, database)
    orm(
        out,
        database,
        PEOPLE
        + """
from api_client import Api, problem
from browser import Browser
from iki_server.signatures.models import Signature, Signing

analyst = person("analyst", project)
curator = person("curator")
RegistryRole.objects.create(registry=project.registry, user=curator, role="registrar")
record = entities.create(
    analyst, "acme/lab.Reagent", project, {"code": "NACL-01", "cas": "7647-14-5"},
).entity
entry = Entity.objects.get(pk=record.entry_id)
browser = Browser()
browser.force_login(analyst)
api = Api(browser)
path = f"/entities/{record.pk}"
body = {"field": "status", "state": "in_review", "acknowledge": True, "return_to": path}


def start(reviewed, *, send_entry=True):
    payload = dict(body)
    if send_entry:
        payload["entry_revision"] = reviewed.json()["entry"]["revision"]
    return api.post(f"{path}/signatures", payload, headers={"If-Match": reviewed["ETag"]})


reviewed = api.get(path)
assert reviewed.status_code == 200, reviewed.content
assert reviewed.json()["values"]["cas"] == "7647-14-5"
missing = problem(start(reviewed, send_entry=False), 422, "validation_failed")
assert missing["errors"][0]["pointer"] == "#/entry_revision"
changed = entries.curate(
    curator, entry, {"cas": "7758-02-3"}, expected_revision=entry.revision,
    reason="Correct the registry fact",
)
stale = start(reviewed)
assert "registry entry" in problem(stale, 412, "stale")["detail"]
assert stale["ETag"] == reviewed["ETag"]
assert not Signing.objects.exists()

reviewed_again = api.call("GET", path, headers={"If-None-Match": reviewed["ETag"]})
assert reviewed_again.status_code == 200, reviewed_again.content
assert reviewed_again["ETag"] == reviewed["ETag"]
assert reviewed_again.json()["values"]["cas"] == "7758-02-3"
assert reviewed_again.json()["entry"]["revision"] == changed.number
started = start(reviewed_again)
assert started.status_code == 201, started.content
signing = Signing.objects.get(pk=started.json()["id"])
assert signing.entry_revision == changed.number
assert signing.entry_revision_reviewed
entries.curate(
    curator, entry, {"cas": "7647-14-5"}, expected_revision=changed.number,
    reason="Reinstate the confirmed registry fact",
)
done = reauthenticate(analyst, signing)
assert done.status == "failed", done.message
assert "changed while you were signing in" in done.message
assert not Signature.objects.exists()

current = api.get(path)
started = start(current)
assert started.status_code == 201, started.content
legacy = Signing.objects.get(pk=started.json()["id"])
Signing.objects.filter(pk=legacy.pk).update(entry_revision_reviewed=False)
done = reauthenticate(analyst, legacy)
assert done.status == "failed", done.message
assert "confirm the registry entry revision" in done.message
assert not Signature.objects.exists()

started = start(current)
assert started.status_code == 201, started.content
done = reauthenticate(analyst, Signing.objects.get(pk=started.json()["id"]))
assert done.status == "signed", done.message
assert done.signature.entry_revision == current.json()["entry"]["revision"]
assert all(c.ok for c in signatures.verify(done.signature))

body.update(state="approved", index=0)
current = api.get(path)
started = start(current)
assert started.status_code == 201, started.content
pending = reauthenticate(analyst, Signing.objects.get(pk=started.json()["id"]))
assert pending.status == "signed" and pending.signature is not None
assert api.get(path).json()["values"]["status"] == "in_review"
entries.curate(
    curator, entry, {"cas": "7758-02-3"}, expected_revision=current.json()["entry"]["revision"],
    reason="Correct the registry fact again",
)
withdrawal = api.post(f"/signatures/{pending.signature_id}/withdrawal", {"reason": "Withdraw"})
assert "lapsed" in problem(withdrawal, 409, "refused")["detail"]
result = None
""",
    )


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
RegistryRole.objects.create(registry=project.registry, user=curator, role="registrar")
# Screens registers a reagent by a mistyped code, and signs its record into review.
wrong = entities.create(
    analyst, "acme/lab.Reagent", screens,
    {"code": "NACL-0l", "cas": "7647-14-5", "note": "Opened 2026-11-02"},
).entity
right = entities.create(
    analyst, "acme/lab.Reagent", project, {"code": "NACL-01", "cas": "7647-14-5"}
).entity
wrong_entry = Entity.objects.get(pk=wrong.entry_id)
reference = entities.create(analyst, "acme/lab.Reagent", project, {"code": "REF-01"}).entity
entries.curate(curator, wrong_entry, {"reference": str(reference.entry_id)},
               expected_revision=wrong_entry.revision, reason="Reference standard")
sign(analyst, wrong)
wrong_entry.refresh_from_db()
entries.curate(curator, wrong_entry, {"reference": None},
               expected_revision=wrong_entry.revision, reason="Retire the reference standard")
from pathlib import Path
from tempfile import TemporaryDirectory
from iki_server.bundles import export
from iki_server.signatures.models import Signature
exporter = person("exporter")
screens.memberships.create(user=exporter, role=ProjectRole.ADMIN)
with TemporaryDirectory() as tmp:
    path = Path(tmp) / "archive.tar.gz"
    def write():
        return export.write(screens, [wrong], path, by=User.objects.get(pk=exporter.pk),
                            reason="Keep the signed history", selection=wrong.hrid)
    try:
        write()
    except export.ExportError as exc:
        assert str(exc) == "This bundle refers to a record you can't read."
    else:
        raise AssertionError("Export requires access to the signed reference standard")
    assert not path.exists()
    project.memberships.create(user=exporter, role=ProjectRole.VIEWER)
    assert reference.entry_id in write().references

before = {c.name: c.ok for c in signatures.verify(Signature.objects.get(entity_id=wrong.pk))}
# A registrar supersedes the mistyped entry; Screens moves its record onto the right one.
entries.supersede(curator, wrong_entry, Entity.objects.get(pk=right.entry_id), reason="Typo")
entries.move(analyst, Entity.objects.get(pk=wrong.pk), reason="Superseded")
moved = Entity.objects.get(pk=wrong.pk)
from api_client import Api
from browser import Browser
browser = Browser()
browser.force_login(analyst)
api = Api(browser)
shown = api.get(f"/entities/{moved.pk}/signatures", verify=True)
assert shown.status_code == 200, shown.content
[item] = shown.json()["items"]
assert item["verification"]["valid"] and item["document"] is None
assert str(wrong_entry.pk).encode() not in shown.content
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
        "WHERE e.id = h.entity_id AND e.hrid = %s AND h.revision = 2",
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
            "SMP-000001’s revision 1 still holds what was signed, read through what was done "
            "to it since: its type renamed acme/lab.Specimen.",
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
