# ruff: noqa: F811 - test_browser's fixtures, imported by name, are parameters here.
"""Compute → Images in real browsers (ADR 0209): a deployment whose compute.yaml binds one
environment to its own registry's image, leaves another unbound, and keeps a pin from an
earlier plugin release. Each state reads in place, in both themes, on a phone and a laptop,
and passes axe. Nobody but a Compute admin sees any of it, or can read it from the API."""

import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from iki_test_plugins import ensure_built
from org_world import World
from playwright.sync_api import Page
from real_server import Server, app_role, manage, serve, server_env
from test_browser import (  # noqa: F401 - fixtures
    _orm_beside_playwright,
    assert_clean,
    browser,
    browser_name,
    open_as,
    playwright,
    world,
)

from iki_server.runs import catalog

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.built_web_app]

NO_SIDEWAYS_SCROLL = "document.documentElement.scrollWidth - window.innerWidth <= 1"
SIZES = [(390, 844), (1440, 900)]

BOUND = "acme/prediction.colabfold"
UNBOUND = "acme/prediction.alphafold-gpu"
DIGEST = "3f1c9a7e5b2d4c6a8e0f1b3d5a7c9e1f2b4d6a8c0e2f4a6b8d0c2e4f6a8b0c2d"
IMAGE = f"registry.lab.example.org/licensed/colabfold@sha256:{DIGEST}"
OLD_IMAGE = "registry.lab.example.org/licensed/colabfold@sha256:" + "9" * 64


def _customer_images(plugins: Path) -> None:
    """Both of acme/prediction's provisioned environments take the deployment's image."""
    for name in ("colabfold", "alphafold-gpu"):
        file = plugins / "structure_prediction" / "environments" / f"{name}.yaml"
        text = re.sub(r"(?m)^image: .*\n", "", file.read_text(encoding="utf-8"))
        file.write_text(text + "customer_image_required: true\n", encoding="utf-8")


@pytest.fixture(scope="module")
def compute_file(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """colabfold bound for the installed 0.4 release, and a pin left from 0.2 and 0.3."""
    data = json.loads(Path(os.environ["IKI_COMPUTE_FILE"]).read_text())
    data["image_bindings"] = [
        {
            "environment": BOUND,
            "plugin_version": ">=0.4,<0.5",
            "image": IMAGE,
            "pull_secret": "lab-registry",
        },
        {
            "environment": BOUND,
            "plugin_version": ">=0.2,<0.4",
            "image": OLD_IMAGE,
            "pull_secret": None,
        },
    ]
    path = tmp_path_factory.mktemp("compute") / "compute.json"
    path.write_text(json.dumps(data))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("IKI_COMPUTE_FILE", str(path))
        catalog.compute.cache_clear()
        yield path
    catalog.compute.cache_clear()


@pytest.fixture(scope="module")
def server(
    browser_app_assets: None,
    django_db_setup: None,
    django_db_blocker: Any,
    tmp_path_factory: pytest.TempPathFactory,
    compute_file: Path,
) -> Iterator[Server]:
    """The server, with those plugins and that compute."""
    from django.conf import settings

    with django_db_blocker.unblock(), app_role() as role:
        built = ensure_built("customer-images", _customer_images)
        plugins = os.pathsep.join(p for p in (str(built), os.environ.get("PYTHONPATH")) if p)
        env = server_env(
            role,
            tmp_path_factory.mktemp("static"),
            IKI_SECRET_KEY=settings.SECRET_KEY,
            PYTHONPATH=plugins,
            IKI_COMPUTE_FILE=str(compute_file),
        )
        env["IKI_PUBLIC_URL"] = f"http://127.0.0.1:{env['IKI_PORT']}"
        done = manage("collectstatic", "--noinput", env=env)
        assert done.returncode == 0, done.stdout + done.stderr
        with serve(env) as running:
            yield running


def card(page: Page, name: str) -> Any:
    return page.get_by_role("region", name=re.compile(f"^{re.escape(name)}"))


def line(region: Any, term: str) -> Any:
    return region.locator("dt", has_text=term).locator("xpath=following-sibling::dd[1]")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_compute_admin_reads_each_environments_image_and_the_unused_pins(
    open_as: Any, color_scheme: str
) -> None:
    page = open_as("compute_admin", "/admin/compute/", color_scheme)
    tabs = page.get_by_role("navigation", name="Compute")
    tabs.get_by_role("link", name="Images").click()
    page.wait_for_url("**/admin/compute/images/")

    bound = card(page, "Colabfold")
    image = line(bound, "Image")
    image.get_by_text("colabfold@sha256:3f1c9a7e5b2d…").wait_for()
    assert DIGEST not in image.inner_text()
    assert image.locator("code").get_attribute("title") == IMAGE
    assert line(bound, "For versions").inner_text() == ">=0.4,<0.5"
    assert line(bound, "Pull Secret").inner_text() == "lab-registry"
    assert line(bound, "Plugin").inner_text() == (
        "Acme/prediction acme/prediction, version 0.4.0 installed"
    )
    assert bound.get_by_text("No image bound").count() == 0

    unbound = card(page, "Alphafold gpu")
    assert unbound.get_by_role("heading", name="No image bound").is_visible()
    assert "until a Compute admin adds an EnvironmentImageBinding" in unbound.inner_text()
    example = unbound.get_by_role("region", name="Example binding for Alphafold gpu")
    assert f"environment: {UNBOUND}\n" in example.inner_text()
    assert 'plugin_version: "==0.4.0"' in example.inner_text()
    assert unbound.get_by_text('A range such as ">=0.4,<0.5" reuses the image').is_visible()

    unused = page.get_by_role("region", name="Unused bindings")
    (row,) = unused.get_by_role("listitem").all()
    assert "For versions >=0.2,<0.4. Version 0.4.0 is installed, outside this range." in (
        row.inner_text()
    )
    assert row.locator("code[title]").get_attribute("title") == OLD_IMAGE

    for width, height in SIZES:
        page.set_viewport_size({"width": width, "height": height})
        page.evaluate("() => new Promise((done) => requestAnimationFrame(() => done(true)))")
        assert page.evaluate(NO_SIDEWAYS_SCROLL), (width, height)
        # The Copy buttons stay inside their cards, beside what they copy.
        for region in (bound, unbound, unused):
            box = region.bounding_box()
            for button in region.get_by_role("button").all():
                at = button.bounding_box()
                assert box and at and at["x"] + at["width"] <= box["x"] + box["width"] + 1
        assert_clean(page, axe_in_firefox=False)
    page.context.close()


def test_the_page_draws_its_cards_once_where_they_stay(open_as: Any) -> None:
    """The tabs above hold still, and the cards appear whole in their final place: nothing
    pressable moves while the bindings load."""
    page = open_as("compute_admin", "/admin/compute/images/")
    held: list[Any] = []

    def listing(url: str) -> bool:
        return url.endswith("/api/v1/compute/image-bindings")

    def hold(route: Any) -> None:
        held.append(route)

    page.route(listing, hold)
    page.reload()
    page.get_by_text("Loading images…").wait_for()
    deadline = time.monotonic() + 10
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert held
    tab = page.get_by_role("navigation", name="Compute").get_by_role("link", name="Images")
    before = tab.bounding_box()
    for route in held:
        route.continue_()
    page.unroute(listing)
    copy = card(page, "Colabfold").get_by_role("button", name=re.compile("^Copy"))
    copy.wait_for()
    drawn = copy.bounding_box()
    page.evaluate(
        "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
    )
    assert tab.bounding_box() == before
    assert copy.bounding_box() == drawn
    page.context.close()


def test_nobody_else_sees_or_reads_the_images(open_as: Any, world: World) -> None:
    for principal in ("viewer", "runner"):
        page = open_as(principal, "/admin/compute/images/")
        page.get_by_role("heading", level=1).filter(has_text="Not found").or_(
            page.get_by_role("heading", name="The admin area isn’t open to you", level=1)
        ).wait_for()
        assert page.get_by_role("link", name="Images").count() == 0
        assert page.get_by_text("No image bound").count() == 0
        assert page.get_by_text("lab-registry").count() == 0
        status = page.evaluate(
            "() => fetch('/api/v1/compute/image-bindings').then((r) => r.status)"
        )
        assert status == 403
        page.context.close()
