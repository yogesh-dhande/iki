"""The entities API: types and their schemas, creating, reading, changing, history, projects."""

from typing import Any

import jsonschema
import pytest
from api_client import Api, problem
from org_world import World, build_world
from test_oauth import bearer, signed_in_token

from iki_server.entities.models import Entity
from iki_server.org.models import Project, ProjectMembership, ProjectRole
from iki_server.webhooks import services as webhooks
from iki_server.webhooks.models import Delivery

pytestmark = pytest.mark.django_db


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def other(world: World) -> Project:
    """A second project: the editor and the outsider are Editors there."""
    project = Project.objects.create(team=world.team, name="Enzymes")
    for name in ("editor", "outsider"):
        ProjectMembership.objects.create(
            project=project, user=world.users[name], role=ProjectRole.EDITOR
        )
    return project


def api_as(world: World, principal: str = "editor") -> Api:
    return Api(world.browser(principal))


def create(
    api: Api,
    project: Project,
    values: dict[str, Any],
    type_name: str = "acme/dna.DNASequence",
    **kw: Any,
) -> Any:
    return api.post(
        "/entities", {"type": type_name, "project": str(project.pk), "values": values, **kw}
    )


def patch(api: Api, ref: str, body: dict[str, Any], etag: str | None) -> Any:
    return api.patch(f"/entities/{ref}", body, headers={"If-Match": etag} if etag else None)


# --- Types -------------------------------------------------------------------------------------


def test_types_come_with_a_schema_of_their_values(world: World) -> None:
    api = api_as(world, "viewer")
    names = [t["name"] for t in api.get("/entity-types").json()]
    assert names == sorted(names)
    assert {"acme/dna.Sequence", "acme/dna.DNASequence", "acme/protein.Protein"} <= set(names)
    assert "iki/core.Entity" not in names
    dna = api.get("/entity-types/acme/dna.DNASequence").json()
    assert dna["extends"] == "acme/dna.Sequence"
    assert dna["identity"] == ["bases"]
    assert dna["hrid_prefix"] == "DNA"
    assert dna["annotations"] == {"gxp_classification": "GAMP 5 category 4"}
    fields = {f["name"]: f for f in dna["fields"]}
    # Inherited from iki/core.Sequence, by way of acme/dna.Sequence.
    assert fields["name"]["plugin"] == "iki/core"
    assert fields["length"]["computed"] and fields["length"]["unit"] == "{bp}"
    schema = dna["values_schema"]
    jsonschema.Draft202012Validator.check_schema(schema)
    check = jsonschema.Draft202012Validator(schema)
    assert check.is_valid({"bases": "ACGT", "topology": "circular", "name": "pUC19"})
    assert not check.is_valid({"bases": "ACGU"})
    assert not check.is_valid({"name": "no bases"})
    assert not check.is_valid({"bases": "ACGT", "colour": "blue"})
    assert schema["properties"]["length"]["readOnly"] is True
    assert schema["properties"]["bases"]["x-iki-immutable"] is True
    sequence = api.get("/entity-types/acme/dna.Sequence").json()
    assert sequence["annotations"] == {}
    assert sequence["abstract"] and "acme/protein.Protein" in sequence["subtypes"]
    problem(api.get("/entity-types/acme/dna.Nothing"), 404, "not_found")


def test_a_name_in_a_path_can_have_its_slash_escaped(world: World) -> None:
    # Iki's own clients send a name's `/` as it is; others may escape it (ADR 0113).
    api = api_as(world, "viewer")
    raw = api.get("/entity-types/acme/dna.DNASequence")
    escaped = api.get("/entity-types/acme%2Fdna.DNASequence")
    assert raw.status_code == escaped.status_code == 200
    assert raw.json() == escaped.json()
    action = api.get("/actions/acme%2Fprotein.compute_properties")
    assert action.status_code == 200
    assert action.json()["name"] == "acme/protein.compute_properties"
    # Without its namespace, the name reaches the view, which says it doesn't know it.
    problem(api.get("/entity-types/dna.DNASequence"), 404, "not_found")
    problem(api.get("/actions/protein.compute_properties"), 404, "not_found")


# --- Creating ----------------------------------------------------------------------------------


def test_creating_answers_201_with_its_etag_and_location(world: World) -> None:
    response = create(
        api_as(world), world.project, {"bases": "ACGT", "name": "pUC19"}, reason="New"
    )
    assert response.status_code == 201, response.content
    body = response.json()
    assert response["ETag"] == '"1"'
    assert response["Location"] == f"/api/v1/entities/{body['id']}"
    assert body["hrid"].startswith("DNA-") and body["revision"] == 1
    assert body["values"]["topology"] == "linear"  # the default
    assert body["values"]["name"] == "pUC19"
    assert body["owner_project"]["id"] == str(world.project.pk)
    assert body["created_by"]["email"] == "editor@example.com"
    assert body["can"] == {
        "edit": True,
        "correct": False,
        "edit_amount": False,
        "draw": False,
        "draw_purposes": [],
        "states": {},
        "sign": [],
        "take": [],
    }
    assert not body["reused"] and not body["archived"]


def test_invalid_values_point_at_their_field(world: World) -> None:
    api = api_as(world)
    body = problem(
        create(api, world.project, {"bases": "XYZ", "length": 4}), 422, "validation_failed"
    )
    assert {e["pointer"]: e["detail"] for e in body["errors"]} == {
        "#/values/bases": "isn't a valid dna sequence.",
        "#/values/length": "is computed: only actions set it.",
    }
    body = problem(
        create(api, world.project, {}, type_name="acme/dna.Nope"), 422, "validation_failed"
    )
    assert body["errors"] == [
        {"pointer": "#/type", "detail": "There's no entity type acme/dna.Nope."}
    ]


def test_registering_again_gives_each_project_its_record(world: World, other: Project) -> None:
    api = api_as(world)
    first = create(api, world.project, {"bases": "GATTACA"}).json()
    assert first["registered"] and first["entry"]["hrid"] == first["hrid"]
    fields = {f["name"]: f for f in api.get("/entity-types/acme/dna.DNASequence").json()["fields"]}
    assert fields["bases"]["registry"] == "identity" and fields["length"]["registry"] == "derived"
    assert fields["name"]["registry"] is None
    # Another project gets its own record of the same entry, with its ID.
    again = create(api, other, {"bases": "GATTACA"})
    assert again.status_code == 201
    theirs = again.json()
    assert theirs["id"] != first["id"] and theirs["hrid"] == first["hrid"]
    assert theirs["entry"]["id"] == first["entry"]["id"]
    assert not theirs["reused"] and not theirs["registered"]
    # The same project gets the record it has, as it is.
    same = create(api_as(world, "outsider"), other, {"bases": "GATTACA", "name": "x"})
    assert same.status_code == 200 and same.json()["reused"]
    assert same.json()["id"] == theirs["id"] and same.json()["values"].get("name") is None
    # Each record is in its own project alone.
    projects = api.get(f"/entities/{first['id']}/projects").json()
    assert [(p["project"]["name"], p["owner"]) for p in projects] == [("Kinase Atlas", True)]


def test_quantities_can_come_in_any_convertible_unit(world: World) -> None:
    api = api_as(world)
    made = create(
        api,
        world.project,
        {"sequence": "MKV", "temperature_optimum": {"value": 310.15, "unit": "K"}},
        type_name="acme/protein.Protein",
    ).json()
    assert made["values"]["temperature_optimum"] == pytest.approx(37.0)
    body = problem(
        create(
            api,
            world.project,
            {"sequence": "MKW", "temperature_optimum": {"value": 1, "unit": "g"}},
            type_name="acme/protein.Protein",
        ),
        422,
        "validation_failed",
    )
    assert body["errors"][0]["pointer"] == "#/values/temperature_optimum"
    # A unit raised past any real one's power is refused before it's worked out.
    body = problem(
        create(
            api,
            world.project,
            {"sequence": "MKW", "temperature_optimum": {"value": 1, "unit": "10*1000000000.K"}},
            type_name="acme/protein.Protein",
        ),
        422,
        "validation_failed",
    )
    assert body["errors"][0] == {
        "pointer": "#/values/temperature_optimum",
        "detail": "has a unit Iki can't use here: `1000000000` is too large an exponent: a "
        "unit's are at most 99.",
    }


# --- Reading -----------------------------------------------------------------------------------


def test_entities_are_found_by_id_or_hrid_and_cached_by_etag(world: World) -> None:
    made = create(api_as(world), world.project, {"bases": "ACGT"}).json()
    api = api_as(world, "viewer")
    by_hrid = api.get(f"/entities/{made['hrid'].lower()}")
    assert by_hrid.status_code == 200 and by_hrid.json()["id"] == made["id"]
    assert by_hrid.json()["can"] == {
        "edit": False,
        "correct": False,
        "edit_amount": False,
        "draw": False,
        "draw_purposes": [],
        "states": {},
        "sign": [],
        "take": [],
    }
    plain = create(api_as(world), world.project, {"name": "Freezer"}, "acme/protein.Freezer").json()
    same = api.call("GET", f"/entities/{plain['id']}", headers={"If-None-Match": '"1"'})
    assert same.status_code == 304 and same["ETag"] == '"1"'
    assert same.content == b""
    # Nobody else can tell it exists.
    for principal in ("outsider", "org_admin", "team_member"):
        problem(api_as(world, principal).get(f"/entities/{made['id']}"), 404, "not_found")
        problem(api_as(world, principal).get(f"/entities/{made['hrid']}"), 404, "not_found")


def test_listing_filters_by_type_with_subtypes_project_and_archived(
    world: World, other: Project
) -> None:
    api = api_as(world)
    dna = create(api, world.project, {"bases": "AAAA"}).json()
    protein = create(api, other, {"sequence": "MKV"}, type_name="acme/protein.Protein").json()
    archived = create(api, world.project, {"bases": "TTTT"}).json()
    patch(api, archived["id"], {"archived": True}, '"1"')

    def ids(**query: Any) -> list[str]:
        response = api.get("/entities", **query)
        assert response.status_code == 200, response.content
        return [e["id"] for e in response.json()["items"]]

    assert ids() == [dna["id"], protein["id"], archived["id"]]
    assert ids(type="acme/dna.Sequence") == [dna["id"], protein["id"], archived["id"]]
    assert ids(type="acme/dna.Sequence", exact_type="true") == []
    assert ids(type="acme/protein.Protein") == [protein["id"]]
    assert ids(project=str(other.pk)) == [protein["id"]]
    assert ids(archived="false") == [dna["id"], protein["id"]]
    assert ids(archived="true") == [archived["id"]]
    listed = api.get("/entities", type="acme/protein.Protein").json()["items"][0]
    assert listed["values"]["sequence"] == "MKV"
    problem(api.get("/entities", type="acme/dna.Nope"), 422, "validation_failed")
    # The viewer reads only the first project's.
    viewer = [e["id"] for e in api_as(world, "viewer").get("/entities").json()["items"]]
    assert viewer == [dna["id"], archived["id"]]


def test_an_owner_project_you_cant_see_is_null(world: World, other: Project) -> None:
    made = create(api_as(world), world.project, {"bases": "CCGG"}).json()
    api_as(world).put(f"/entities/{made['id']}/projects/{other.pk}")
    seen = api_as(world, "outsider").get(f"/entities/{made['id']}").json()
    assert seen["owner_project"] is None and seen["can"] == {
        "edit": False,
        "correct": False,
        "edit_amount": False,
        "draw": False,
        "draw_purposes": [],
        "states": {},
        "sign": [],
        "take": [],
    }
    projects = api_as(world, "outsider").get(f"/entities/{made['id']}/projects").json()
    assert [p["project"]["name"] for p in projects] == ["Enzymes"]


# --- Changing ----------------------------------------------------------------------------------


def test_changes_need_the_etag_and_refuse_a_stale_one(world: World) -> None:
    api = api_as(world)
    ref = create(api, world.project, {"bases": "ACGT"}).json()["id"]
    problem(patch(api, ref, {"values": {"name": "a"}}, None), 428, "precondition_required")
    # One change is made from one reading: `*`, a list or a weak ETag names none (ADR 0289).
    for unusable in ("*", '"1", "2"', 'W/"1"', "1"):
        refused = patch(api, ref, {"values": {"name": "a"}}, unusable)
        problem(refused, 428, "precondition_required")
    changed = patch(api, ref, {"values": {"name": "a"}, "reason": "Typo"}, '"1"')
    assert changed.status_code == 200, changed.content
    assert changed["ETag"] == '"2"' and changed.json()["values"]["name"] == "a"
    # Someone still holding revision 1 is told, and given the current ETag.
    stale = patch(api, ref, {"values": {"name": "b"}}, '"1"')
    assert problem(stale, 412, "stale")["detail"].startswith("Someone changed it")
    assert stale["ETag"] == '"2"'
    # An ETag Iki never gave matches no revision.
    problem(patch(api, ref, {"values": {"name": "b"}}, '"two"'), 412, "stale")
    assert patch(api, ref, {"values": {"name": "b"}}, '"2"').status_code == 200


def test_edit_rules_answer_422(world: World) -> None:
    api = api_as(world)
    ref = create(api, world.project, {"bases": "ACGT"}).json()["id"]
    body = problem(
        patch(api, ref, {"values": {"bases": "GGGG", "gc_content": 1, "colour": "x"}}, '"1"'),
        422,
        "validation_failed",
    )
    assert {e["pointer"]: e["detail"] for e in body["errors"]} == {
        "#/values/bases": "can't change once it's set.",
        "#/values/gc_content": "is computed: only actions set it.",
        "#/values/colour": "acme/dna.DNASequence has no field `colour`.",
    }
    assert api.get(f"/entities/{ref}").json()["revision"] == 1


def test_archived_entities_change_only_after_restoring(world: World) -> None:
    api = api_as(world)
    ref = create(api, world.project, {"bases": "ACGT"}).json()["id"]
    archived = patch(api, ref, {"archived": True, "reason": "Retired"}, '"1"')
    assert archived.json()["archived"] and archived["ETag"] == '"2"'
    problem(patch(api, ref, {"values": {"name": "x"}}, '"2"'), 409, "refused")
    both = patch(api, ref, {"archived": False, "values": {"name": "x"}}, '"2"')
    assert both.status_code == 200, both.content
    assert not both.json()["archived"] and both.json()["values"]["name"] == "x"
    assert both["ETag"] == '"4"'
    kinds = [r["kind"] for r in api.get(f"/entities/{ref}/revisions").json()["items"]]
    assert kinds == ["edited", "restored", "archived", "created"]


def test_nothing_to_change_still_checks_the_etag(world: World) -> None:
    api = api_as(world)
    ref = create(api, world.project, {"bases": "ACGT"}).json()["id"]
    assert patch(api, ref, {}, '"1"').status_code == 200
    assert patch(api, ref, {"values": {"topology": "linear"}}, '"1"')["ETag"] == '"1"'
    problem(patch(api, ref, {}, '"7"'), 412, "stale")


# --- History -----------------------------------------------------------------------------------


def test_revisions_show_who_what_how_and_why(world: World) -> None:
    api = api_as(world)
    ref = create(api, world.project, {"bases": "ACGT", "name": "a"}).json()["id"]
    patch(api, ref, {"values": {"name": "b", "topology": "circular"}, "reason": "Rename"}, '"1"')
    revisions = api.get(f"/entities/{ref}/revisions").json()["items"]
    assert [r["number"] for r in revisions] == [2, 1]
    latest = revisions[0]
    assert latest["changed_fields"] == ["name", "topology"]
    assert latest["reason"] == "Rename" and latest["via"] == "web"  # the browser's session
    assert latest["user"]["email"] == "editor@example.com"
    first = api.get(f"/entities/{ref}/revisions/1").json()
    assert first["values"]["name"] == "a" and first["values"]["topology"] == "linear"
    second = api.get(f"/entities/{ref}/revisions/2").json()
    assert second["changes"] == {
        "name": {"before": "a", "after": "b"},
        "topology": {"before": "linear", "after": "circular"},
    }
    problem(api.get(f"/entities/{ref}/revisions/3"), 404, "not_found")
    provenance = api.get(f"/entities/{ref}/provenance").json()
    assert provenance["name"]["number"] == 2 and provenance["bases"]["number"] == 1


def test_a_projects_changes_are_its_records_newest_first_and_no_one_elses(
    world: World, other: Project
) -> None:
    """A project's page shows its latest changes (ADR 0176): its records' revisions, its own
    and those linked into it, newest first. Another project's records never appear, and only
    the project's members may ask."""
    editor = api_as(world)
    own = create(editor, world.project, {"bases": "ACGT", "name": "pUC19"}).json()
    patch(editor, own["id"], {"values": {"name": "pUC19c"}, "reason": "Rename"}, '"1"')
    elsewhere = create(editor, other, {"bases": "TTAA", "name": "Enzyme only"}).json()
    shared = create(editor, other, {"bases": "GGCC", "name": "Shared in"}).json()
    assert editor.put(f"/entities/{shared['id']}/projects/{world.project.pk}").status_code == 201

    url = f"/projects/{world.project.pk}/changes"
    changes = api_as(world, "viewer").get(url).json()
    assert [(c["entity"]["hrid"], c["number"], c["kind"]) for c in changes] == [
        (shared["hrid"], 1, "created"),
        (own["hrid"], 2, "edited"),
        (own["hrid"], 1, "created"),
    ]
    latest_edit = changes[1]
    assert latest_edit["entity"]["title"] == "pUC19c"  # as it's called now
    assert latest_edit["changed_fields"] == ["name"]
    assert latest_edit["reason"] == "Rename"
    assert latest_edit["user"]["email"] == "editor@example.com"
    assert elsewhere["hrid"] not in {c["entity"]["hrid"] for c in changes}

    limited = api_as(world, "viewer").get(f"{url}?limit=1")
    assert [c["entity"]["hrid"] for c in limited.json()] == [shared["hrid"]]
    problem(editor.get(f"{url}?limit=51"), 422, "validation_failed")

    # The outsider is in Enzymes, not Kinase Atlas: it isn't there for them.
    problem(api_as(world, "outsider").get(url), 404, "not_found")
    # An Org Admin sees the project exists, not what changed in it.
    problem(api_as(world, "org_admin").get(url), 403, "forbidden")
    # Enzymes' changes are Enzymes' records': the shared one, and its own.
    theirs = api_as(world, "outsider").get(f"/projects/{other.pk}/changes").json()
    assert {c["entity"]["hrid"] for c in theirs} == {elsewhere["hrid"], shared["hrid"]}


def test_tokens_need_write_to_change_and_are_held_to_their_projects(
    world: World, other: Project
) -> None:
    ref = create(api_as(world), world.project, {"bases": "ACGT"}).json()["id"]
    elsewhere = create(api_as(world), other, {"bases": "TTAA"}).json()["id"]
    _, tokens = signed_in_token(world, scope="read", projects=[world.project])
    token = tokens["access_token"]
    assert bearer(token, "GET", f"/entities/{ref}").status_code == 200
    problem(bearer(token, "GET", f"/entities/{elsewhere}"), 404, "not_found")
    response = bearer(
        token, "PATCH", f"/entities/{ref}", {"values": {"name": "x"}}, {"If-Match": '"1"'}
    )
    problem(response, 403, "insufficient_scope")
    _, tokens = signed_in_token(world, scope="read write", projects=[world.project])
    response = bearer(
        tokens["access_token"],
        "PATCH",
        f"/entities/{ref}",
        {"values": {"name": "x"}},
        {"If-Match": '"1"'},
    )
    assert response.status_code == 200, response.content
    revisions = api_as(world).get(f"/entities/{ref}/revisions").json()["items"]
    assert revisions[0]["via"] == "api"


# --- Projects ----------------------------------------------------------------------------------


def test_linking_and_unlinking(world: World, other: Project) -> None:
    api = api_as(world)
    ref = create(api, world.project, {"bases": "ACGT"}).json()["id"]
    linked = api.put(f"/entities/{ref}/projects/{other.pk}")
    assert linked.status_code == 201 and not linked.json()["owner"]
    assert api.put(f"/entities/{ref}/projects/{other.pk}").status_code == 200
    # The outsider reads it through Enzymes, but only the owner project's Editors change it.
    outsider = api_as(world, "outsider")
    assert outsider.get(f"/entities/{ref}").status_code == 200
    problem(patch(outsider, ref, {"values": {"name": "x"}}, '"1"'), 403, "forbidden")
    # They can take it out of their own project, though.
    assert outsider.delete(f"/entities/{ref}/projects/{other.pk}").status_code == 204
    problem(outsider.get(f"/entities/{ref}"), 404, "not_found")
    problem(api.delete(f"/entities/{ref}/projects/{other.pk}"), 404, "not_found")
    problem(api.delete(f"/entities/{ref}/projects/{world.project.pk}"), 409, "refused")


# --- Webhooks ----------------------------------------------------------------------------------


def test_changes_reach_webhooks_without_their_values(world: World, other: Project) -> None:
    webhooks.create(
        project=world.project,
        url="https://hooks.example.com/iki",
        description="",
        chosen=["entity.created", "entity.updated", "entity.archived", "entity.linked"],
        by=world.users["admin"],
    )
    api = api_as(world)
    made = create(api, world.project, {"bases": "ACGT", "name": "secret"}).json()
    patch(api, made["id"], {"values": {"name": "still secret"}, "reason": "R"}, '"1"')
    patch(api, made["id"], {"archived": True}, '"2"')
    sent = list(Delivery.objects.exclude(event="ping").order_by("pk"))
    assert [d.event for d in sent] == ["entity.created", "entity.updated", "entity.archived"]
    data = sent[1].payload["data"]
    assert data["entity"] == {
        "id": made["id"],
        "hrid": made["hrid"],
        "type": "acme/dna.DNASequence",
        "revision": 2,
        "owner_project": {"id": str(world.project.pk)},
        "entry": {"id": made["entry"]["id"]},
        "archived": False,
    }
    assert data["revision"] == {
        "number": 2,
        "kind": "edited",
        "changed_fields": ["name"],
        "via": "web",
        "reason": "R",
    }
    assert data["actor"]["email"] == "editor@example.com"
    assert "secret" not in str([d.payload for d in sent])
    # Linking into another project reaches that project's webhooks, not this one's.
    api.put(f"/entities/{made['id']}/projects/{other.pk}")
    assert not Delivery.objects.filter(event="entity.linked").exists()
    assert Entity.objects.filter(pk=made["id"]).exists()


def test_file_fields_take_a_file_the_caller_can_read(world: World) -> None:
    from scan_fixtures import clean_blob

    from iki_server.files.models import File

    blob = clean_blob(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    pdb = File.objects.create(
        blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=world.project
    )
    notes = File.objects.create(
        blob=blob, name="notes.txt", media_type="text/plain", project=world.project
    )
    api = api_as(world)
    made = create(api, world.project, {"structure": str(pdb.pk)}, "acme/protein.ProteinStructure")
    assert made.status_code == 201, made.content
    assert made.json()["values"]["structure"] == str(pdb.pk)
    body = problem(
        create(api, world.project, {"structure": str(notes.pk)}, "acme/protein.ProteinStructure"),
        422,
        "validation_failed",
    )
    assert body["errors"] == [
        {
            "pointer": "#/values/structure",
            "detail": "must be chemical/x-mmcif, chemical/x-pdb or Model coordinates, "
            "and this file is text/plain.",
        }
    ]


def test_quantities_say_how_to_show_them_in_their_display_unit(world: World) -> None:
    body = api_as(world, "viewer").get("/entity-types/acme/protein.Protein").json()
    fields = {f["name"]: f for f in body["fields"]}
    weight = fields["molecular_weight"]
    assert (weight["unit"], weight["display_unit"]) == ("u", "ku")
    resolution = {
        f["name"]: f
        for f in api_as(world, "viewer")
        .get("/entity-types/acme/protein.ProteinStructure")
        .json()["fields"]
    }["resolution"]
    assert (resolution["unit"], resolution["unit_symbol"]) == ("Ao", "Å")
    assert weight["display_scale"] == pytest.approx(0.001)
    assert weight["display_offset"] == 0
    assert fields["sequence"]["display_scale"] is None


def test_a_display_unit_thats_no_scale_is_shown_in_the_fields_own(world: World) -> None:
    from iki_server.api.entities import field_out
    from iki_spec.model import Field as SpecField

    # A hydrogen-ion concentration kept in mol/L and read as pH is a valid pair (UCUM
    # converts them), but no scale and offset take one to the other: 0 mol/L has no pH.
    acidity = field_out(
        "acidity",
        SpecField(name="acidity", type="quantity", unit="mol/L", display_unit="[pH]"),
        "acme/protein",
    )
    assert (acidity["display_scale"], acidity["display_offset"]) == (None, None)
    assert acidity["display_unit_symbol"] == "pH"
    # A temperature kept in kelvin and read in Celsius is one: an offset.
    temperature = field_out(
        "temperature",
        SpecField(name="temperature", type="quantity", unit="K", display_unit="Cel"),
        "acme/protein",
    )
    assert temperature["display_scale"] == pytest.approx(1)
    assert temperature["display_offset"] == pytest.approx(-273.15)
    # A sound pressure level is 2 lg of a pressure (UCUM Table 20): 200 dB[SPL] is
    # 20 µPa · 10^(200/20) = 0.2 MPa. Its values at 0, 1 and 2 dB lie within 1e-12 MPa of
    # a line, but it isn't one, so no scale is given that would show 200 dB as 5e-10 MPa.
    loudness = field_out(
        "loudness",
        SpecField(name="loudness", type="quantity", unit="dB[SPL]", display_unit="MPa"),
        "acme/protein",
    )
    assert (loudness["display_scale"], loudness["display_offset"]) == (None, None)


def test_entities_name_what_their_values_refer_to(world: World, other: Project) -> None:
    from scan_fixtures import clean_blob

    from iki_server.files.models import File

    api = api_as(world)
    protein = create(api, world.project, {"sequence": "MKV"}, "acme/protein.Protein").json()
    hidden = create(api, other, {"sequence": "MKW"}, "acme/protein.Protein").json()
    blob = clean_blob(sha256="a" * 64, size=10, storage_uri="s3://iki/a")
    pdb = File.objects.create(
        blob=blob, name="1abc.pdb", media_type="chemical/x-pdb", project=world.project
    )
    # A reference holds a registry entry (ADR 0115) the project has: another project's
    # record, which the editor reads there, isn't one.
    values = {"structure": str(pdb.pk), "chains": [protein["id"], hidden["id"]]}
    refused = problem(
        create(api, world.project, values, "acme/protein.ProteinStructure"),
        422,
        "validation_failed",
    )
    assert "isn't in Kinase Atlas" in refused["errors"][0]["detail"]
    values = {"structure": str(pdb.pk), "chains": [protein["id"]]}
    made = create(api, world.project, values, "acme/protein.ProteinStructure")
    assert made.status_code == 201, made.content
    assert made.json()["values"]["chains"] == [protein["entry"]["id"]]
    # Named by the entry, as the project's record of it is.
    seen = api_as(world, "viewer").get(f"/entities/{made.json()['id']}").json()
    assert seen["refs"] == {
        protein["entry"]["id"]: {
            "hrid": protein["hrid"],
            "title": protein["hrid"],
            "type": "acme/protein.Protein",
            "archived": False,
            "purged": False,
            "formerly": [],
            "code": None,
            "superseded": None,
        }
    }
    assert seen["files"] == {
        str(pdb.pk): {"name": "1abc.pdb", "media_type": "chemical/x-pdb", "size": 10}
    }
    listed = api_as(world, "viewer").get("/entities", type="acme/protein.ProteinStructure").json()
    assert listed["items"][0]["refs"] == seen["refs"]


def test_entities_have_their_types_display_title(world: World) -> None:
    api = api_as(world)
    named = create(
        api, world.project, {"sequence": "MKV", "name": "Lysozyme"}, "acme/protein.Protein"
    )
    unnamed = create(api, world.project, {"sequence": "MKW"}, "acme/protein.Protein").json()
    assert named.json()["title"] == "Lysozyme"
    assert unnamed["title"] == unnamed["hrid"]
    # References carry their target's title too.
    values = {"sequence": "MKY", "source": named.json()["id"]}
    variant = create(api, world.project, values, "acme/protein.Protein").json()
    assert variant["refs"][named.json()["entry"]["id"]]["title"] == "Lysozyme"


def test_a_reference_to_an_entry_is_titled_by_the_referring_projects_record(
    world: World, other: Project
) -> None:
    # Two projects hold one plasmid, each naming it as it does: a record refers to its own
    # project's, which its link opens, not to whichever was made first.
    puc19 = "TCGCGCGTTTCGGTGATGACGG"  # pUC19's first bases (GenBank L09137).
    editor = api_as(world, "editor")
    first = create(editor, other, {"bases": puc19, "name": "pUC19"}).json()
    mine = create(editor, world.project, {"bases": puc19, "name": "SYNPUC19CV"}).json()
    assert mine["entry"]["id"] == first["entry"]["id"]
    entry = mine["entry"]["id"]
    protein = {"sequence": "MSIQHFRVALIPFFAAFCLPVFA", "encoded_by": mine["id"]}
    made = create(editor, world.project, protein, "acme/protein.Protein").json()
    assert made["refs"][entry]["title"] == "SYNPUC19CV"
    assert editor.get(f"/entities/{made['id']}").json()["refs"][entry]["title"] == "SYNPUC19CV"
    listed = editor.get("/entities", type="acme/protein.Protein").json()["items"]
    assert [e["refs"][entry]["title"] for e in listed] == ["SYNPUC19CV"]
    assert editor.get(f"/entities/{made['id']}/revisions/1").json()["refs"][entry]["title"] == (
        "SYNPUC19CV"
    )
    # In the other project, its record's name.
    theirs = create(
        editor, other, {**protein, "encoded_by": first["id"]}, "acme/protein.Protein"
    ).json()
    assert theirs["refs"][entry]["title"] == "pUC19"


def test_q_finds_entities_by_hrid_or_the_text_in_their_title(world: World) -> None:
    api = api_as(world)
    lysozyme = create(
        api, world.project, {"sequence": "MKV", "name": "Lysozyme C"}, "acme/protein.Protein"
    ).json()
    other = create(
        api, world.project, {"sequence": "MKW", "name": "Insulin"}, "acme/protein.Protein"
    )
    dna = create(api, world.project, {"bases": "ACGT", "name": "lysozyme gene"}).json()

    def found(**query: Any) -> list[str]:
        return [e["hrid"] for e in api.get("/entities", **query).json()["items"]]

    assert found(q="lysoZ", type="acme/protein.Protein") == [lysozyme["hrid"]]
    assert found(q="lysoz") == [lysozyme["hrid"], dna["hrid"]]
    assert found(q=other.json()["hrid"].lower()) == [other.json()["hrid"]]
    # The start of an HRID finds those it starts; a part of one, those it's in.
    prefix = other.json()["hrid"].split("-")[0]
    assert other.json()["hrid"] in found(q=f"{prefix.lower()}-0")
    assert found(q=f"{prefix[1:]}-0") == []
    assert other.json()["hrid"] in found(q=other.json()["hrid"][2:])
    # Subtypes are searched through their parent: a Sequence can be either.
    assert found(q="lysozyme", type="acme/dna.Sequence") == [lysozyme["hrid"], dna["hrid"]]
    assert found(q="  ", type="acme/protein.Protein") == [lysozyme["hrid"], other.json()["hrid"]]


def test_quantity_fields_offer_the_common_units_of_their_kind(world: World) -> None:
    body = api_as(world, "viewer").get("/entity-types/acme/protein.Protein").json()
    fields = {f["name"]: f for f in body["fields"]}
    assert [u["symbol"] for u in fields["molecular_weight"]["input_units"]] == [
        "kDa",
        "Da",
        "g/mol",
        "kg/mol",
    ]
    assert fields["temperature_optimum"]["input_units"] == [
        {"code": "Cel", "symbol": "°C"},
        {"code": "K", "symbol": "K"},
        {"code": "[degF]", "symbol": "°F"},
    ]
    assert fields["sequence"]["input_units"] == []


def test_revisions_name_what_their_changes_referred_to(world: World) -> None:
    api = api_as(world)
    first = create(
        api, world.project, {"sequence": "MKV", "name": "Lysozyme"}, "acme/protein.Protein"
    )
    second = create(api, world.project, {"sequence": "MKW"}, "acme/protein.Protein").json()
    made = create(
        api,
        world.project,
        {"sequence": "MKY", "source": first.json()["id"]},
        "acme/protein.Protein",
    ).json()
    patch(api, made["id"], {"values": {"source": second["id"]}}, '"1"')
    changed = api.get(f"/entities/{made['id']}/revisions/2").json()
    # The entity it no longer refers to is named too, so its history reads.
    assert {r["hrid"] for r in changed["refs"].values()} == {first.json()["hrid"], second["hrid"]}
    assert changed["refs"][first.json()["entry"]["id"]]["title"] == "Lysozyme"
