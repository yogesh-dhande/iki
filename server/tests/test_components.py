"""Components: what installed plugins add to Iki's pages, and their modules (ADR 0084)."""

import base64
import dataclasses
import hashlib
import re

import pytest
from browser import Browser
from django.utils.csp import CSP
from org_world import World
from test_api_entities import api_as
from test_web_security import csp_directives
from test_workers import world

from iki_server.components import installed, views
from iki_server.web import frames

__all__ = ["world"]

pytestmark = pytest.mark.django_db

RESIDUES = "acme/protein.residues"


def test_components_are_listed_with_where_they_appear(world: World) -> None:
    listed = {c["name"]: c for c in api_as(world, "viewer").get("/components").json()}
    found = listed[RESIDUES]
    module = installed.module(RESIDUES)
    assert found == {
        "name": RESIDUES,
        "plugin": "acme/protein",
        "label": "Residues",
        "description": "How many residues a protein has.",
        "element": "protein-residues",
        "slots": [{"slot": "entity.tab", "type": "acme/protein.Protein", "label": "Residues"}],
        "selects": "acme/protein.Protein",
        "sha256": hashlib.sha256(module).hexdigest(),
        "frame": f"/plugin-components/{RESIDUES}/{hashlib.sha256(module).hexdigest()}/",
    }
    assert b"customElements.define" in module


def test_subtypes_pages_show_their_parents_components() -> None:
    assert [c.qualified_name for c in installed.for_type("acme/protein.Protein", "entity.tab")] == [
        RESIDUES
    ]
    assert installed.for_type("acme/dna.DNASequence", "entity.tab") == []


def test_a_module_that_changed_after_the_build_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    component = installed.components()[RESIDUES]
    installed.module.cache_clear()
    changed = dataclasses.replace(component, sha256="0" * 64)
    monkeypatch.setitem(installed.components(), RESIDUES, changed)
    with pytest.raises(installed.ModuleChanged):
        installed.module(RESIDUES)
    installed.module.cache_clear()


def addresses() -> tuple[str, str, bytes]:
    module = installed.module(RESIDUES)
    digest = hashlib.sha256(module).hexdigest()
    base = f"/plugin-components/{RESIDUES}/{digest}"
    return f"{base}/", f"{base}.js", module


def test_a_frame_runs_the_bridge_and_the_module_and_nothing_else() -> None:
    frame, module_url, module = addresses()
    # No session: the frame's document belongs to no one, so it never has one.
    page = Browser().get(frame)
    assert page.status_code == 200
    html = page.content.decode()
    integrity = "sha256-" + base64.b64encode(hashlib.sha256(module).digest()).decode()
    module_src = f"https://testserver{module_url}"
    assert (
        f'<script type="module" src="{module_src}" integrity="{integrity}" '
        'crossorigin="anonymous"></script>'
    ) in html
    config = '<script type="application/json" id="iki-component">{"element": "protein-residues"}'
    assert config in html
    scripts = re.findall(r"<script type=\"module\">(.*?)</script>", html, re.DOTALL)
    assert scripts == [views.BRIDGE]
    csp = csp_directives(page[CSP.HEADER_ENFORCE])
    bridge = "'sha256-" + base64.b64encode(hashlib.sha256(views.BRIDGE.encode()).digest()).decode()
    assert csp["script-src"] == [
        frames.HARDEN_SOURCE,
        bridge + "'",
        module_src,
        "'wasm-unsafe-eval'",
    ]
    # First of all, before any module: what takes WebRTC and frames away (ADR 0102).
    assert html.index(frames.HARDEN_TAG) < html.index('<script type="module"')
    assert csp["default-src"] == ["'none'"]
    assert csp["connect-src"] == ["'none'"]
    assert csp["form-action"] == ["'none'"]
    assert csp["sandbox"] == ["allow-scripts"]
    assert csp["frame-ancestors"] == ["'self'"]
    assert "frame-src" not in csp  # default-src 'none': no frames of its own
    assert page["X-Frame-Options"] == "SAMEORIGIN"
    assert page["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert page["Cache-Control"] == "public, max-age=31536000, immutable"


def test_a_module_is_served_to_frames_of_no_origin() -> None:
    _, module_url, module = addresses()
    response = Browser().get(module_url)
    assert response.status_code == 200
    assert response.content == module
    assert response["Content-Type"] == "text/javascript; charset=utf-8"
    assert response["Access-Control-Allow-Origin"] == "*"
    assert response["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert response["Cache-Control"] == "public, max-age=31536000, immutable"


def test_only_installed_components_at_their_digest_are_served() -> None:
    frame, module_url, _ = addresses()
    browser = Browser()
    for path in (frame, module_url):
        assert browser.get(path.replace(RESIDUES, "acme/protein.gone")).status_code == 404
        digest = path.split("/")[3].removesuffix(".js")
        assert browser.get(path.replace(digest, "0" * 64)).status_code == 404


def test_a_frame_whose_module_changed_isnt_served(monkeypatch: pytest.MonkeyPatch) -> None:
    frame, module_url, _ = addresses()
    installed.module.cache_clear()

    def changed(name: str) -> bytes:
        raise installed.ModuleChanged(name)

    monkeypatch.setattr(installed, "module", changed)
    for path in (frame, module_url):
        assert Browser().get(path).status_code == 404
