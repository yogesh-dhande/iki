"""Lab lists (ADR 0270): dropdowns a lab curates itself. A list's options are registry
entries every project in the registry picks from; its registrars and the list's curators
add, relabel, reorder, archive and supersede them, each with a reason, audited; a record
keeps an option archived since; a plugin's starting options seed each registry, and an
upgrade never undoes what a lab did to them."""

import dataclasses
from csv import reader as csv_rows
from typing import Any

import pytest
from api_client import Api, problem
from org_world import World, build_world, post
from test_oauth import bearer, signed_in_token

from iki_server.audit.models import AuditEvent
from iki_server.entities import lists, registry
from iki_server.entities.models import Entity
from iki_server.notifications.models import Notification
from iki_server.org.models import (
    ListCurator,
    Project,
    ProjectMembership,
    ProjectRole,
    Registry,
)
from iki_spec.model import ListSpec

pytestmark = pytest.mark.django_db

METHOD = "acme/protein.PurificationMethod"
CATEGORY = "acme/protein.MethodCategory"
PREP = "acme/protein.Prep"
OPTIONS = f"/lists/{METHOD}/options"


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def rival(world: World) -> Project:
    """Another project in the Kinase Atlas's registry, where only the outsider is."""
    project = Project.objects.create(team=world.team, name="Rival Screens")
    ProjectMembership.objects.create(
        project=project, user=world.users["outsider"], role=ProjectRole.ADMIN
    )
    return project


def api_as(world: World, principal: str) -> Api:
    return Api(world.browser(principal))


def registrar(world: World, principal: str = "auditor") -> Api:
    admin = api_as(world, "org_admin")
    user = world.users[principal]
    put = admin.put(f"/registries/{Registry.default().pk}/registrars/{user.pk}")
    assert put.status_code in (200, 201), put.content
    return api_as(world, principal)


def curator(world: World, principal: str = "auditor", type_name: str = METHOD) -> Api:
    admin = api_as(world, "org_admin")
    user = world.users[principal]
    put = admin.put(f"/registries/{Registry.default().pk}/list-curators/{type_name}/{user.pk}")
    assert put.status_code in (200, 201), put.content
    return api_as(world, principal)


def options(api: Api, project: Project | None, **query: Any) -> list[dict[str, Any]]:
    """A list's options as a project's member reads them, or with no project as its
    registry's curators do."""
    at = {"project": str(project.pk)} if project is not None else scope()
    response = api.get(OPTIONS, **at, **query)
    assert response.status_code == 200, response.content
    found: list[dict[str, Any]] = response.json()
    return found


def option(api: Api, project: Project | None, code: str) -> dict[str, Any]:
    return next(o for o in options(api, project, archived=True) if o["code"] == code)


def prep(api: Api, project: Project, values: dict[str, Any]) -> Any:
    return api.post("/entities", {"type": PREP, "project": str(project.pk), "values": values})


def scope(world: World | None = None) -> dict[str, str]:
    return {"registry": str(Registry.default().pk)}


# --- Starting options and reading ------------------------------------------------------------


def test_a_registry_starts_with_the_plugins_options_in_order(world: World) -> None:
    editor = api_as(world, "editor")
    listed = editor.get("/lists", project=str(world.project.pk))
    assert listed.status_code == 200, listed.content
    by_type = {item["type"]: item for item in listed.json()}
    assert by_type[METHOD]["options"] == 3 and by_type[CATEGORY]["options"] == 1
    assert by_type[METHOD]["label_plural"] == "Purification methods"
    assert not by_type[METHOD]["can"]["curate"]
    found = options(editor, world.project)
    assert [(o["code"], o["label"], o["position"]) for o in found] == [
        ("protein_a", "Protein A affinity", 1),
        ("sec", "Size exclusion", 2),
        ("dialysis", "Dialysis", 3),
    ]
    assert found[0]["description"].startswith("Captured on a Protein A column")
    # A starting option's reference to another list names that list's option by its ID.
    category = editor.get(f"/lists/{CATEGORY}/options", project=str(world.project.pk)).json()
    assert found[0]["values"]["category"] == category[0]["id"]
    assert found[2]["values"].get("category") is None
    # Members don't see how often each is used: that's for its curators.
    assert all(o["uses"] is None for o in found)
    # The entity types say it's a list, for forms to show a dropdown.
    types = {t["name"]: t for t in editor.get("/entity-types").json()}
    assert types[METHOD]["lab_list"] and not types[PREP]["lab_list"]
    # Each starting option's first revision says where it came from.
    first = editor.get(f"/entries/{found[0]['id']}/revisions").json()["items"][-1]
    assert first["kind"] == "created" and first["user"] is None
    assert first["reason"] == "A starting option of acme/protein 3.0.0."


def test_seeding_again_adds_only_what_a_registry_lacks(world: World) -> None:
    """An upgrade's new starting options are added at the end; a relabel, a reorder or an
    archive the lab made stays as it is, and what the lab added keeps its label."""
    admin = registrar(world)
    sec = option(admin, None, "sec")
    relabelled = admin.patch(
        f"{OPTIONS}/sec",
        {"label": "SEC (Superdex 200)", "reason": "The column we use"},
        headers={"If-Match": f'"{sec["revision"]}"'},
        **scope(world),
    )
    assert relabelled.status_code == 200, relabelled.content
    archived = admin.post(
        f"{OPTIONS}/dialysis/archive", {"reason": "We desalt instead"}, **scope(world)
    )
    assert archived.status_code == 200, archived.content
    ours = admin.post(OPTIONS, {"code": "imac", "label": "Ni-NTA"}, **scope(world))
    assert ours.status_code == 201, ours.content
    method = registry.types()[METHOD]
    assert method.list_spec is not None
    upgraded = dataclasses.replace(
        method,
        list_spec=ListSpec(
            options=(
                *method.list_spec.options,
                {"code": "hic", "label": "Hydrophobic interaction", "category": "chromatography"},
                {"code": "nickel", "label": "ni-nta"},
            )
        ),
    )
    every = dict(registry.types())
    every[METHOD] = upgraded
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry, "types", lambda: every)
        report = lists.seed()
    assert report == [f"{METHOD} in {Registry.default().name}: hic"]
    found = options(admin, None, archived=True)
    assert [(o["code"], o["label"], o["archived"]) for o in found] == [
        ("protein_a", "Protein A affinity", False),
        ("sec", "SEC (Superdex 200)", False),
        ("dialysis", "Dialysis", True),
        ("imac", "Ni-NTA", False),
        ("hic", "Hydrophobic interaction", False),
    ]
    assert found[-1]["position"] == 5
    assert lists.seed() == []


def test_every_project_in_the_registry_reads_its_lists_and_no_other(
    world: World, rival: Project
) -> None:
    outsider = api_as(world, "outsider")
    mine = options(api_as(world, "editor"), world.project)
    theirs = options(outsider, rival)
    assert [o["id"] for o in theirs] == [o["id"] for o in mine]
    # Reading an option as an entry works for any project in the registry.
    assert outsider.get(f"/entries/{mine[0]['id']}").status_code == 200
    # Another registry has options of its own, which the first's projects don't see.
    other = Registry.objects.create(name="Partner registry", all_teams=False)
    other.teams.add(world.team)
    partner = Project.objects.create(team=world.team, name="Partner work", registry=other)
    ProjectMembership.objects.create(
        project=partner, user=world.users["team_member"], role=ProjectRole.EDITOR
    )
    separate = options(api_as(world, "team_member"), partner)
    assert [o["code"] for o in separate] == ["protein_a", "sec", "dialysis"]
    assert {o["id"] for o in separate}.isdisjoint(o["id"] for o in mine)
    problem(outsider.get(OPTIONS, project=str(partner.pk)), 404, "not_found")
    problem(outsider.get(OPTIONS, registry=str(other.pk)), 404, "not_found")
    problem(outsider.get(f"/entries/{separate[0]['id']}"), 404, "not_found")
    problem(api_as(world, "editor").get(OPTIONS), 422, "validation_failed")
    problem(
        api_as(world, "editor").get(f"/lists/{PREP}/options", project=str(world.project.pk)),
        404,
        "not_found",
    )


# --- Picking --------------------------------------------------------------------------------


def test_a_record_picks_an_option_and_shows_its_code_and_label(world: World) -> None:
    editor = api_as(world, "editor")
    found = {o["code"]: o for o in options(editor, world.project)}
    made = prep(
        editor,
        world.project,
        {
            "name": "Anti-HER2 prep 12",
            "method": found["protein_a"]["id"],
            "polishing": [found["sec"]["id"]],
            "steps": [{"method": found["dialysis"]["id"], "note": "Overnight, 4 °C"}],
        },
    )
    assert made.status_code == 201, made.content
    body = made.json()
    assert body["values"]["method"] == found["protein_a"]["id"]
    ref = body["refs"][found["protein_a"]["id"]]
    assert ref["code"] == "protein_a" and ref["title"] == "Protein A affinity"
    assert not ref["superseded"]
    # An option is nobody's record: it isn't made through /entities.
    problem(
        editor.post(
            "/entities",
            {"type": METHOD, "project": str(world.project.pk), "values": {"code": "x"}},
        ),
        422,
        "validation_failed",
    )
    # Nor does a member curate the list.
    problem(
        editor.post(OPTIONS, {"code": "hplc", "label": "HPLC"}, project=str(world.project.pk)),
        403,
        "forbidden",
    )


def test_an_archived_option_stays_on_the_records_that_have_it(world: World) -> None:
    editor, admin = api_as(world, "editor"), registrar(world)
    dialysis = option(editor, world.project, "dialysis")
    made = prep(editor, world.project, {"name": "Prep 7", "method": dialysis["id"]}).json()
    archived = admin.post(
        f"{OPTIONS}/dialysis/archive", {"reason": "Replaced by desalting"}, **scope(world)
    )
    assert archived.status_code == 200 and archived.json()["archived"], archived.content
    assert "dialysis" not in [o["code"] for o in options(editor, world.project)]
    # A new record can't pick it.
    refused = problem(
        prep(editor, world.project, {"name": "Prep 8", "method": dialysis["id"]}),
        422,
        "validation_failed",
    )
    assert "archived" in str(refused["errors"])
    # A record that has it keeps it through an edit of something else.
    entity = editor.get(f"/entities/{made['id']}")
    edited = editor.patch(
        f"/entities/{made['id']}",
        {"values": {"name": "Prep 7 (re-run)"}, "reason": "Renamed"},
        headers={"If-Match": entity["ETag"]},
    )
    assert edited.status_code == 200, edited.content
    assert edited.json()["values"]["method"] == dialysis["id"]
    # Search still finds records by it.
    searched = editor.post(
        "/entities/search",
        {"type": PREP, "where": {"field": "method", "op": "eq", "value": dialysis["id"]}},
    )
    assert searched.status_code == 200, searched.content
    assert [e["id"] for e in searched.json()["items"]] == [made["id"]]
    restored = admin.post(f"{OPTIONS}/dialysis/restore", {"reason": "Back in use"}, **scope(world))
    assert restored.status_code == 200 and not restored.json()["archived"]
    assert [o["code"] for o in options(editor, world.project)][-1] == "dialysis"
    actions = list(
        AuditEvent.objects.filter(action__startswith="list.option_").values_list(
            "action", "data__reason"
        )
    )
    assert ("list.option_archived", "Replaced by desalting") in actions
    assert ("list.option_restored", "Back in use") in actions


# --- Curating -------------------------------------------------------------------------------


def test_registrars_and_the_lists_curators_curate_it(world: World) -> None:
    # An Org Admin appoints curators, but isn't one by being an Org Admin.
    org_admin = api_as(world, "org_admin")
    problem(
        org_admin.post(OPTIONS, {"code": "hplc", "label": "HPLC"}, **scope(world)), 403, "forbidden"
    )
    lead = curator(world, "team_member")
    added = lead.post(
        OPTIONS,
        {
            "code": "hplc",
            "label": "Reversed-phase HPLC",
            "values": {"sop": "https://sop.example/12"},
        },
        **scope(world),
    )
    assert added.status_code == 201, added.content
    body = added.json()
    assert (body["position"], body["values"]["sop"]) == (4, "https://sop.example/12")
    # They see how often each option is used, and curate only their own list.
    assert all(o["uses"] == 0 for o in lead.get(OPTIONS, **scope(world)).json())
    problem(
        lead.post(f"/lists/{CATEGORY}/options", {"code": "x", "label": "X"}, **scope(world)),
        403,
        "forbidden",
    )
    listed = lead.get("/lists", **scope(world)).json()
    assert {i["type"]: i["can"]["curate"] for i in listed} == {METHOD: True, CATEGORY: False}
    curators = org_admin.get(f"/registries/{Registry.default().pk}/list-curators").json()
    assert [(c["list"], c["user"]["id"]) for c in curators] == [
        (METHOD, str(world.users["team_member"].pk))
    ]
    # Dismissed, they don't.
    gone = org_admin.delete(
        f"/registries/{Registry.default().pk}/list-curators/{METHOD}/{world.users['team_member'].pk}"
    )
    assert gone.status_code == 204, gone.content
    problem(
        lead.post(OPTIONS, {"code": "uf", "label": "Ultrafiltration"}, **scope(world)),
        403,
        "forbidden",
    )
    assert AuditEvent.objects.filter(action="registry.list_curator_appointed").count() == 1
    assert AuditEvent.objects.filter(action="registry.list_curator_removed").count() == 1
    assert AuditEvent.objects.get(action="list.option_added").data["code"] == "hplc"


@pytest.mark.parametrize("appointed", [registrar, curator])
def test_a_curators_token_curates_only_with_the_admin_scope_and_every_project(
    world: World, appointed: Any
) -> None:
    """ADR 0270: a list's options are every project's in the registry, so a registrar's or
    a curator's token curates them only with the admin scope, as a registry's curation
    needs, and only when it was given every project; their browser session curates."""
    appointed(world, "editor")
    at = f"{OPTIONS}?registry={Registry.default().pk}"

    def add(code: str, token: str | None = None) -> Any:
        body = {"code": code, "label": code.upper()}
        if token is None:
            return api_as(world, "editor").post(OPTIONS, body, **scope(world))
        return bearer(token, "POST", at, body)

    def token(scopes: str, **choice: Any) -> str:
        _, tokens = signed_in_token(world, "editor", scope=scopes, **choice)
        given: str = tokens["access_token"]
        return given

    assert add("hplc").status_code == 201
    assert add("iex", token("read write admin")).status_code == 201
    problem(add("hic", token("read write")), 403, "insufficient_scope")
    limited = add("tff", token("read write admin", projects=[world.project]))
    assert limited.status_code == 403, limited.content
    # A token that can't curate changes nothing, and reading still works with it.
    codes = {o["code"] for o in options(api_as(world, "editor"), None)}
    assert {"hplc", "iex"} <= codes and not {"hic", "tff"} & codes
    listed = bearer(token("read"), "GET", at)
    assert listed.status_code == 200, listed.content
    # Archiving needs the same.
    archive = f"/lists/{METHOD}/options/iex/archive?registry={Registry.default().pk}"
    refused = bearer(token("read write"), "POST", archive, {"reason": "x"})
    problem(refused, 403, "insufficient_scope")
    done = bearer(token("read write admin"), "POST", archive, {"reason": "Not used here"})
    assert done.status_code == 200, done.content


def test_org_admins_appoint_a_lists_curators_on_its_registrys_page(world: World) -> None:
    admin = world.browser("org_admin")
    page = f"/admin/registries/{Registry.default().pk}/"
    shown = admin.get(page).content.decode()
    assert 'id="list-curators-title"' in shown
    # Each installed list, by its label, linked to its page in this registry.
    assert f'href="/types/{METHOD}/?registry={Registry.default().pk}"' in shown
    assert "No curators of its own: its registrars curate it." in shown
    lead = world.users["team_member"]
    appointed = post(
        admin,
        page,
        {"action": "appoint_curator", "curator-list_type": METHOD, "curator-person": lead.email},
    )
    assert appointed.status_code == 302, appointed.content.decode()[:2000]
    assert appointed["Location"].endswith("#list-curators-title")
    assert ListCurator.objects.filter(list_type=METHOD, user=lead).exists()
    shown = admin.get(page).content.decode()
    assert "Remove Team Member as a curator of Purification methods" in shown
    # A deactivated person is refused, at the field, and the page says why.
    refused = post(
        admin,
        page,
        {
            "action": "appoint_curator",
            "curator-list_type": CATEGORY,
            "curator-person": world.users["inactive"].email,
        },
    )
    assert refused.status_code == 200
    assert "is deactivated" in refused.content.decode()
    # Only a list's a choice.
    bogus = post(
        admin,
        page,
        {"action": "appoint_curator", "curator-list_type": PREP, "curator-person": lead.email},
    )
    assert bogus.status_code == 200 and "Select a valid choice" in bogus.content.decode()
    dismissed = post(
        admin, page, {"action": "dismiss_curator", "list": METHOD, "user": str(lead.pk)}
    )
    assert dismissed.status_code == 302
    assert not ListCurator.objects.filter(list_type=METHOD, user=lead).exists()
    assert AuditEvent.objects.filter(action="registry.list_curator_removed").count() == 1
    # It's an Org Admin's page: nobody else's.
    assert world.browser("team_member").get(page).status_code in (302, 403)


def test_a_code_is_once_a_lists_and_a_label_once_in_any_case(world: World) -> None:
    admin = registrar(world)
    again = problem(
        admin.post(OPTIONS, {"code": "sec", "label": "Gel filtration"}, **scope(world)),
        422,
        "validation_failed",
    )
    assert "in the list already" in str(again["errors"])
    same = problem(
        admin.post(OPTIONS, {"code": "gf", "label": "size EXCLUSION"}, **scope(world)),
        422,
        "validation_failed",
    )
    assert "`sec`'s label" in str(same["errors"])
    bad = problem(
        admin.post(OPTIONS, {"code": "two words", "label": "Two words"}, **scope(world)),
        422,
        "validation_failed",
    )
    assert bad["errors"]
    # Another list may use the same code.
    other = admin.post(
        f"/lists/{CATEGORY}/options", {"code": "sec", "label": "Separation"}, **scope(world)
    )
    assert other.status_code == 201, other.content


def test_a_relabel_needs_a_reason_and_keeps_the_code(world: World) -> None:
    editor, admin = api_as(world, "editor"), registrar(world)
    sec = option(admin, None, "sec")
    made = prep(editor, world.project, {"name": "Prep 3", "method": sec["id"]}).json()
    problem(
        admin.patch(
            f"{OPTIONS}/sec",
            {"label": "SEC", "reason": ""},
            headers={"If-Match": f'"{sec["revision"]}"'},
            **scope(world),
        ),
        422,
        "validation_failed",
    )
    stale = admin.patch(
        f"{OPTIONS}/sec",
        {"label": "SEC", "reason": "Shorter"},
        headers={"If-Match": '"99"'},
        **scope(world),
    )
    problem(stale, 412, "stale")
    code = problem(
        admin.patch(
            f"{OPTIONS}/sec",
            {"values": {"code": "gf"}, "reason": "Rename"},
            headers={"If-Match": f'"{sec["revision"]}"'},
            **scope(world),
        ),
        422,
        "validation_failed",
    )
    assert "supersede" in str(code["errors"])
    done = admin.patch(
        f"{OPTIONS}/sec",
        {"label": "SEC (Superdex 200)", "reason": "Name the column"},
        headers={"If-Match": f'"{sec["revision"]}"'},
        **scope(world),
    )
    assert done.status_code == 200, done.content
    assert (done.json()["code"], done.json()["label"]) == ("sec", "SEC (Superdex 200)")
    # The record shows what it's called now; the option's history says what it was.
    seen = editor.get(f"/entities/{made['id']}").json()
    assert seen["refs"][sec["id"]]["title"] == "SEC (Superdex 200)"
    revisions = editor.get(f"/entries/{sec['id']}/revisions").json()["items"]
    assert revisions[0]["kind"] == "curated" and revisions[0]["reason"] == "Name the column"
    event = AuditEvent.objects.get(action="list.option_changed")
    assert event.data["list"] == METHOD and event.data["reason"] == "Name the column"


def test_reordering_names_every_option_once(world: World) -> None:
    admin, editor = registrar(world), api_as(world, "editor")
    admin.post(f"{OPTIONS}/dialysis/archive", {"reason": "Not used"}, **scope(world))
    problem(
        admin.put(
            f"/lists/{METHOD}/order", {"codes": ["sec", "protein_a"], "reason": "x"}, **scope(world)
        ),
        422,
        "validation_failed",
    )
    done = admin.put(
        f"/lists/{METHOD}/order",
        {"codes": ["sec", "dialysis", "protein_a"], "reason": "Most used first"},
        **scope(world),
    )
    assert done.status_code == 200, done.content
    assert [o["code"] for o in done.json()] == ["sec", "dialysis", "protein_a"]
    assert [o["code"] for o in options(editor, world.project)] == ["sec", "protein_a"]
    event = AuditEvent.objects.get(action="list.reordered")
    assert event.data["changes"]["order"]["before"] == ["protein_a", "sec", "dialysis"]
    assert event.data["changes"]["order"]["after"] == ["sec", "dialysis", "protein_a"]


def test_a_wrong_code_is_superseded_and_each_project_moves_its_records(
    world: World, rival: Project
) -> None:
    editor, outsider, admin = api_as(world, "editor"), api_as(world, "outsider"), registrar(world)
    wrong = admin.post(OPTIONS, {"code": "protien_g", "label": "Protein G"}, **scope(world)).json()
    mine = prep(editor, world.project, {"name": "Prep 1", "method": wrong["id"]}).json()
    prep(outsider, rival, {"name": "Rival prep", "method": wrong["id"]})
    renamed = admin.patch(
        f"{OPTIONS}/protien_g",
        {"label": "Protein G (typo)", "reason": "Freeing the label"},
        headers={"If-Match": f'"{wrong["revision"]}"'},
        **scope(world),
    )
    assert renamed.status_code == 200, renamed.content
    right = admin.post(OPTIONS, {"code": "protein_g", "label": "Protein G"}, **scope(world)).json()
    # Curators see how often it's used, never by which records.
    assert option(admin, None, "protien_g")["uses"] == 2
    done = admin.post(
        f"{OPTIONS}/protien_g/supersede",
        {"replacement": "protein_g", "reason": "Typo in the code"},
        **scope(world),
    )
    assert done.status_code == 200, done.content
    assert done.json()["superseded_by"]["code"] == "protein_g"
    assert "protien_g" not in [o["code"] for o in options(editor, world.project)]
    # Each referring project's Admins are told.
    told = set(
        Notification.objects.filter(kind="registry.entry_superseded").values_list(
            "recipient_id", flat=True
        )
    )
    assert {world.users["admin"].pk, world.users["outsider"].pk} <= told
    # By the options' labels, opening the option's page with the project, to move them.
    notice = Notification.objects.get(
        kind="registry.entry_superseded", recipient=world.users["admin"]
    )
    assert notice.title == "Purification method “Protein G (typo)” was superseded by “Protein G”"
    assert notice.path == f"/entries/{wrong['id']}/?project={world.project.slug}"
    # A record that names it shows it as superseded until its project moves it.
    ref = editor.get(f"/entities/{mine['id']}").json()["refs"][wrong["id"]]
    assert ref["superseded"]
    problem(
        api_as(world, "viewer").post(
            f"{OPTIONS}/protien_g/move", {"project": str(world.project.pk), "reason": "x"}
        ),
        403,
        "forbidden",
    )
    moved = editor.post(
        f"{OPTIONS}/protien_g/move",
        {"project": str(world.project.pk), "reason": "Typo in the code"},
    )
    assert moved.status_code == 200, moved.content
    assert moved.json()["moved"] == [mine["hrid"]]
    assert editor.get(f"/entities/{mine['id']}").json()["values"]["method"] == right["id"]
    # The rival's record is the rival's to move.
    assert option(admin, None, "protien_g")["uses"] == 1
    assert AuditEvent.objects.get(action="list.references_moved").data["repointed"] == [
        mine["hrid"]
    ]


def test_a_download_names_an_option_by_its_code(world: World) -> None:
    """A relabel changes what people see, not what a download says: its code."""
    editor, admin = api_as(world, "editor"), registrar(world)
    sec = option(editor, world.project, "sec")
    prep(editor, world.project, {"name": "Prep 5", "method": sec["id"]})
    admin.patch(
        f"{OPTIONS}/sec",
        {"label": "Gel filtration", "reason": "What the lab calls it"},
        headers={"If-Match": f'"{sec["revision"]}"'},
        **scope(world),
    )
    csv = (
        editor.get("/entities/search.csv", type=PREP, columns=["hrid", "name", "method"])
        .getvalue()
        .decode()
    )
    rows = list(csv_rows(csv.splitlines()))
    assert (rows[0][2], rows[1][2]) == ("Method", "sec"), csv
    assert lists.codes_of([sec["id"]]) == {Entity.objects.get(pk=sec["id"]).pk: "sec"}


def test_a_sheet_names_an_option_by_its_code_or_label(world: World) -> None:
    from iki_server.entities.values import shape
    from iki_server.spreadsheets.services import Resolver

    admin = registrar(world)
    admin.post(f"{OPTIONS}/dialysis/archive", {"reason": "Not used"}, **scope(world))
    found = {
        o["code"]: o["id"] for o in options(api_as(world, "editor"), world.project, archived=True)
    }
    method = shape(PREP).fields()["method"]
    resolved = Resolver(world.users["editor"], world.project).find(
        method, {"sec", "PROTEIN A AFFINITY", "dialysis", "hplc"}
    )
    assert str(resolved["sec"]) == found["sec"]
    assert str(resolved["PROTEIN A AFFINITY"]) == found["protein_a"]
    # An archived one is found, and refused as it's saved unless the record has it already.
    assert str(resolved["dialysis"]) == found["dialysis"]
    assert resolved["hplc"] == "is no option of Purification methods: give its code or its label."


def test_a_sheets_cell_shows_an_option_by_its_label(world: World) -> None:
    """The sheet's cell, and what its check says it read, are the option's label: what
    people pick, which the sheet reads back as it reads the option's code."""
    editor = api_as(world, "editor")
    made = prep(editor, world.project, {"name": "Prep 9"}).json()
    answer = editor.post(
        f"/projects/{world.project.pk}/sheet-edits/check",
        {
            "type": PREP,
            "rows": [
                {
                    "key": made["id"],
                    "entity": made["id"],
                    "revision": made["revision"],
                    "cells": {"method": "sec", "polishing": "dialysis; PROTEIN A AFFINITY"},
                }
            ],
        },
    )
    assert answer.status_code == 200, answer.content
    row = answer.json()["rows"][0]
    assert row["problems"] == [], row
    assert row["shown"]["method"] == "Size exclusion"
    assert row["shown"]["polishing"] == "Dialysis; Protein A affinity"
