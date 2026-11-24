"""The built site's check finds what a reader, or a browser's policy, would trip over."""

from __future__ import annotations

from pathlib import Path

from sitegen import check

REPOSITORY = "https://github.com/yogesh-dhande/iki"
POLICY = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "script-src 'self'; style-src 'self'\">"
)
FRAME_POLICY = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "script-src 'self'; style-src 'self' 'unsafe-inline'\">"
)


def page(body: str = "", head: str = "", policy: str = POLICY) -> str:
    return (
        f'<!doctype html><html><head><meta charset="utf-8">{policy}'
        f'<link rel="stylesheet" href="/style.css">{head}</head><body>{body}</body></html>'
    )


def site(tmp_path: Path) -> check.Site:
    return check.Site(tmp_path / "dist", "https://ikisci.org", REPOSITORY, "/viewers/")


def test_a_clean_site_has_no_problems(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "guide").mkdir(parents=True)
    (dist / "viewers" / "map").mkdir(parents=True)
    (dist / "style.css").write_text("body { background: url(/dot.svg); }")
    (dist / "dot.svg").write_text("<svg/>")
    (dist / "index.html").write_text(
        page(
            '<h2 id="here">Here</h2><a href="/guide/#there">a</a><a href="#here">b</a>'
            f'<a href="{REPOSITORY}/blob/main/Makefile">c</a><a href="https://example.com/">d</a>'
            '<iframe src="/viewers/map/"></iframe>'
        )
    )
    (dist / "guide" / "index.html").write_text(page('<h2 id="there">There</h2>'))
    (dist / "viewers" / "map" / "index.html").write_text(page(policy=FRAME_POLICY))
    assert check.problems(site(tmp_path)) == []


def test_the_check_finds_what_a_reader_would_trip_over(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "guide").mkdir(parents=True)
    (dist / "style.css").write_text("body { background: url(https://cdn.example/b.png); }")
    (dist / "index.html").write_text(
        page(
            '<h2 id="here">Here</h2><a href="/guide/#nowhere">a</a><a href="/missing/">b</a>'
            f'<a href="{REPOSITORY}/blob/main/Gone.md">c</a>'
            '<button onclick="go()">d</button><a href="javascript:go()">e</a>',
            head='<script src="https://cdn.example/a.js"></script>',
        )
    )
    (dist / "guide" / "index.html").write_text(
        page('<h2 id="there">There</h2>', policy="", head=POLICY)
    )
    (dist / "inline.html").write_text(page(policy=FRAME_POLICY))
    (dist / "bare.html").write_text('<!doctype html><meta charset="utf-8"><p>Hi</p>')

    assert sorted(check.problems(site(tmp_path))) == sorted(
        [
            "index.html: <button onclick> is an inline event handler",
            "index.html: <a href> is a javascript: URL",
            "index.html: <script src> loads https://cdn.example/a.js from another origin",
            "index.html: /guide/#nowhere names #nowhere, which isn't on its page",
            "index.html: /missing/ goes nowhere in the site",
            f"index.html: {REPOSITORY}/blob/main/Gone.md names a file that isn't in the repository",
            "guide/index.html: the Content Security Policy comes after ['link']",
            "inline.html: only viewers' frames may allow inline styles",
            "bare.html: no Content Security Policy",
            "style.css: loads https://cdn.example/b.png from another origin",
        ]
    )


def test_a_policy_must_refuse_inline_scripts_and_start_from_nothing(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "style.css").write_text("")
    loose = (
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'self'; script-src 'self' 'unsafe-inline'\">"
    )
    (dist / "index.html").write_text(page(policy=loose))
    assert sorted(check.problems(site(tmp_path))) == [
        "index.html: the Content Security Policy allows inline scripts",
        "index.html: the Content Security Policy doesn't start from default-src 'none'",
    ]


def test_nothing_built_is_a_problem(tmp_path: Path) -> None:
    found = check.problems(site(tmp_path))
    assert len(found) == 1
    assert found[0].startswith("Nothing is built in")
