"""Checks a built static site (``docs/site/dist`` by default) as a reader's browser meets it.

- Every link inside the site goes to a page or file the build made, and every
  ``#fragment`` to an element on that page.
- Every link to a file in Iki's repository names a file that's there.
- Nothing is loaded from another origin: no scripts, styles, fonts, images or frames
  from CDNs, and nothing that tracks readers.
- Every page carries its Content Security Policy before anything it governs, never
  allows inline scripts by ``'unsafe-inline'``, and has no inline event handlers or
  ``javascript:`` URLs, which the policy would block. Only the frames that hold the
  interactive viewers (``--frames``) may allow inline styles: the viewers add their own.

Any docs site in the repository can use it: ``python -m sitegen check DIR --origin URL``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from sitegen.repo import ORIGIN, REPOSITORY, ROOT

#: Elements whose attribute loads something into the page, rather than link to it.
LOADS = {
    ("script", "src"),
    ("img", "src"),
    ("source", "src"),
    ("iframe", "src"),
    ("video", "src"),
    ("audio", "src"),
    ("embed", "src"),
    ("object", "data"),
}
#: ``<link rel>`` values that load what they name.
LOADING_RELS = {"stylesheet", "preload", "modulepreload", "icon", "shortcut", "manifest"}
CSS_URL = re.compile(r"""url\(\s*['"]?(?P<url>[^'")\s]+)|@import\s+['"](?P<import>[^'"]+)""")
URL_ATTRIBUTES = ("href", "src", "data", "action", "srcset", "poster")


@dataclass(frozen=True)
class Site:
    """A built site: where it is, where it's served, and the repository it links to."""

    dist: Path
    origin: str = ORIGIN
    repository: str = REPOSITORY
    #: The path under which pages may allow inline styles: the viewers' frames.
    frames: str = "/viewers/"


class Page(HTMLParser):
    """What a check needs from one HTML file."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        #: (tag, attribute, value, rel) for every URL in the page; rel is a link's.
        self.urls: list[tuple[str, str, str, str]] = []
        self.problems: list[str] = []
        #: The tags in ``<head>`` before the Content Security Policy's ``<meta>``.
        self.before_policy: list[str] = []
        self.policy: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        for name in ("id", "name") if tag == "a" else ("id",):
            if name in values:
                self.ids.add(values[name])
        for name, value in values.items():
            if name.startswith("on"):
                self.problems.append(f"<{tag} {name}> is an inline event handler")
            if name in URL_ATTRIBUTES:
                if value.strip().lower().startswith("javascript:"):
                    self.problems.append(f"<{tag} {name}> is a javascript: URL")
                self.urls.append((tag, name, value, values.get("rel", "")))
        if tag == "meta" and values.get("http-equiv", "").lower() == "content-security-policy":
            self.policy = values.get("content", "")
        elif self.policy is None and tag not in ("html", "head"):
            self.before_policy.append(tag)


def parse(path: Path) -> Page:
    page = Page()
    page.feed(path.read_text(encoding="utf-8"))
    page.close()
    return page


def directives(policy: str) -> dict[str, list[str]]:
    found = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            found[words[0].lower()] = words[1:]
    return found


class Build:
    """A built site's pages, parsed, and the files a URL on it names."""

    def __init__(self, site: Site) -> None:
        self.site = site
        self.pages = {path: parse(path) for path in sorted(site.dist.rglob("*.html"))}

    def url(self, path: Path) -> str:
        relative = path.relative_to(self.site.dist).as_posix()
        return f"{self.site.origin}/{relative.removesuffix('index.html')}"

    def target(self, url: str) -> Path | None:
        """The file a URL on the site names, if the build made it."""
        candidate = self.site.dist / unquote(urlsplit(url).path).lstrip("/")
        if candidate.is_file():
            return candidate
        if (candidate / "index.html").is_file():
            return candidate / "index.html"
        return None


def loads(tag: str, attribute: str, rel: str) -> bool:
    if (tag, attribute) in LOADS:
        return True
    return tag == "link" and bool(LOADING_RELS & set(rel.lower().split()))


def check_policy(build: Build, name: str, page: Page) -> Iterator[str]:
    if page.policy is None:
        yield f"{name}: no Content Security Policy"
        return
    if page.before_policy != ["meta"]:
        yield f"{name}: the Content Security Policy comes after {page.before_policy[1:]}"
    found = directives(page.policy)
    if found.get("default-src") != ["'none'"]:
        yield f"{name}: the Content Security Policy doesn't start from default-src 'none'"
    if "'unsafe-inline'" in found.get("script-src", []):
        yield f"{name}: the Content Security Policy allows inline scripts"
    in_frame = ("/" + name).startswith(build.site.frames)
    if "'unsafe-inline'" in found.get("style-src", []) and not in_frame:
        yield f"{name}: only viewers' frames may allow inline styles"


def check_pages(build: Build) -> Iterator[str]:
    for path, page in build.pages.items():
        here = build.url(path)
        name = path.relative_to(build.site.dist).as_posix()
        for problem in page.problems:
            yield f"{name}: {problem}"
        yield from check_policy(build, name, page)
        for tag, attribute, value, rel in page.urls:
            if name == "404.html" and rel == "canonical":
                continue  # A host serves it for any path that's missing; it has no URL.
            for url in _urls(attribute, value):
                yield from _check_url(build, name, here, tag, attribute, rel, url)


def _check_url(
    build: Build, name: str, here: str, tag: str, attribute: str, rel: str, url: str
) -> Iterator[str]:
    absolute = urljoin(here, url)
    parts = urlsplit(absolute)
    if parts.scheme in ("mailto", "data", "blob", ""):
        return
    if f"{parts.scheme}://{parts.netloc}" != build.site.origin:
        if loads(tag, attribute, rel):
            yield f"{name}: <{tag} {attribute}> loads {url} from another origin"
        repository = re.escape(build.site.repository)
        match = re.match(rf"^{repository}/(?:blob|tree|edit)/[^/]+/(?P<path>[^#?]*)", absolute)
        if match and not (ROOT / unquote(match["path"])).exists():
            yield f"{name}: {url} names a file that isn't in the repository"
        return
    found = build.target(absolute)
    if found is None:
        yield f"{name}: {url} goes nowhere in the site"
    elif (
        parts.fragment
        and found in build.pages
        and unquote(parts.fragment) not in build.pages[found].ids
    ):
        yield f"{name}: {url} names #{unquote(parts.fragment)}, which isn't on its page"


def _urls(attribute: str, value: str) -> list[str]:
    if attribute == "srcset":
        return [item.split()[0] for item in value.split(",") if item.strip()]
    return [value] if value else []


def check_css(build: Build) -> Iterator[str]:
    for path in sorted(build.site.dist.rglob("*.css")):
        for match in CSS_URL.finditer(path.read_text(encoding="utf-8")):
            url = match["url"] or match["import"]
            if re.match(r"^(https?:)?//", url):
                yield f"{path.relative_to(build.site.dist)}: loads {url} from another origin"


def problems(site: Site) -> list[str]:
    if not (site.dist / "index.html").is_file():
        return [f"Nothing is built in {site.dist}: build the site first (make docs)."]
    build = Build(site)
    return [*check_pages(build), *check_css(build)]
