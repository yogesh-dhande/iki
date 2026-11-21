"""Viewers: Iki's own viewers on a type's pages, as the type API lists them, and the frame
the structure viewer runs Mol* in (ADR 0085)."""

import base64
import hashlib
import re

import pytest
from browser import Browser
from conftest import FAKE_VIEWER_FILES
from django.utils.csp import CSP
from org_world import World
from test_api_entities import api_as
from test_web_security import csp_directives
from test_workers import world

from iki_server.web import frames, static_headers, viewers

__all__ = ["world"]

pytestmark = pytest.mark.django_db


def test_a_type_lists_the_viewers_it_configures(world: World) -> None:
    types = {t["name"]: t for t in api_as(world, "viewer").get("/entity-types").json()}
    assert types["acme/protein.Protein"]["viewers"] == [
        {
            "viewer": "sequence",
            "label": "Sequence",
            "sequence": "sequence",
            "alphabet": "protein",
            "strands": None,
            "topology": None,
            "features": None,
            "tracks": [{"field": "hydropathy", "label": "Hydropathy", "min": -4.5, "max": 4.5}],
        }
    ]
    assert types["acme/dna.DNASequence"]["viewers"] == []
    assert types["acme/protein.ProteinStructure"]["viewers"] == [
        {"viewer": "structure", "label": "Structure", "structure": "structure"}
    ]


def test_a_sequence_viewer_says_what_its_sequence_is_of() -> None:
    """DNA's bases, RNA's (with U, which the viewer counts in nt) or amino acids, by the
    field's format: no first-party type has RNA yet, so a plugin's would be the first."""
    from types import SimpleNamespace

    from iki_server.api.entities import _viewer_out
    from iki_spec.model import Viewer

    for format_, alphabet in (
        ("dna_sequence", "dna"),
        ("rna_sequence", "rna"),
        ("protein_sequence", "protein"),
    ):
        form = SimpleNamespace(
            fields=lambda f=format_: {
                "bases": SimpleNamespace(format=f, strands=None, topology=None)
            }
        )
        shown = _viewer_out(form, Viewer(viewer="sequence", label="Sequence", sequence="bases"))  # type: ignore[arg-type]
        assert shown["alphabet"] == alphabet
        assert shown["strands"] is None


def test_a_sequence_viewer_names_the_field_saying_how_many_strands_it_has() -> None:
    """So it counts a single strand in nt and a duplex in bp, as its record says."""
    from types import SimpleNamespace

    from iki_server.api.entities import _viewer_out
    from iki_spec.model import Viewer

    bases = SimpleNamespace(format="dna_sequence", strands="strandedness", topology=None)
    form = SimpleNamespace(fields=lambda: {"bases": bases})
    shown = _viewer_out(form, Viewer(viewer="sequence", label="Sequence", sequence="bases"))  # type: ignore[arg-type]
    assert shown["strands"] == "strandedness"


def test_a_sequence_viewer_names_the_field_saying_whether_its_circular() -> None:
    """So its search for bases reads across the origin of a circle and not a line's ends."""
    from types import SimpleNamespace

    from iki_server.api.entities import _viewer_out
    from iki_spec.model import Viewer

    bases = SimpleNamespace(format="dna_sequence", strands=None, topology="topology")
    form = SimpleNamespace(fields=lambda: {"bases": bases})
    shown = _viewer_out(form, Viewer(viewer="sequence", label="Sequence", sequence="bases"))  # type: ignore[arg-type]
    assert shown["topology"] == "topology"


# --- The structure viewer's frame ---------------------------------------------------------------


def test_the_structure_frame_runs_mol_star_and_nothing_else() -> None:
    # No session: the frame's document belongs to no one, so it never has one.
    page = Browser().get("/viewers/structure/")
    assert page.status_code == 200
    html = page.content.decode()
    name = "viewers/assets/main-test.js"
    module = f"https://testserver/static/{name}"
    digest = hashlib.sha384(FAKE_VIEWER_FILES[name]).digest()
    integrity = "sha384-" + base64.b64encode(digest).decode()
    # First, what takes WebRTC and frames away (ADR 0102); then Mol*.
    assert re.findall(r"<script[^>]*>", html) == [
        "<script>",
        f'<script type="module" src="{module}" integrity="{integrity}" crossorigin="anonymous">',
    ]
    assert frames.HARDEN_TAG in html
    csp = csp_directives(page[CSP.HEADER_ENFORCE])
    assert csp["script-src"] == [frames.HARDEN_SOURCE, module]
    assert csp["default-src"] == ["'none'"]
    assert csp["connect-src"] == ["'none'"]
    assert csp["form-action"] == ["'none'"]
    assert csp["sandbox"] == ["allow-scripts"]
    assert csp["frame-ancestors"] == ["'self'"]
    assert "worker-src" not in csp and "frame-src" not in csp  # default-src 'none'
    assert page["X-Frame-Options"] == "SAMEORIGIN"
    assert page["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert page["Cache-Control"] == "no-cache"


def test_without_its_build_the_structure_frame_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viewers, "MANIFEST", "viewers/missing.json")
    page = Browser().get("/viewers/structure/")
    assert page.status_code == 503
    assert b"make web" in page.content


def test_viewers_code_is_for_frames_of_no_origin_too() -> None:
    headers: dict[str, str] = {}
    static_headers.add_headers(
        headers, "/x/viewers/assets/main-1.js", "/static/viewers/assets/main-1.js"
    )
    assert headers == {"Cross-Origin-Resource-Policy": "cross-origin"}
    others: dict[str, str] = {}
    static_headers.add_headers(others, "/x/app/assets/main-1.js", "/static/app/assets/main-1.js")
    assert others == {}


def test_the_apps_workers_run_under_a_policy_of_their_own() -> None:
    headers: dict[str, str] = {}
    static_headers.add_headers(
        headers,
        "/x/app/assets/worker-reader-1.js",
        "/static/app/assets/worker-reader-1.js",
    )
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self' 'wasm-unsafe-eval'" in csp
    assert "connect-src 'self'" in csp
    assert "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", "")
    maps: dict[str, str] = {}
    static_headers.add_headers(
        maps, "/x/app/assets/worker-reader-1.js.map", "/static/app/assets/worker-reader-1.js.map"
    )
    assert maps == {}
