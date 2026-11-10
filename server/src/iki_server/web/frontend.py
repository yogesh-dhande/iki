"""The web app (React, in ``web/app``): which files a page loads, and how they're stored.

``pnpm --filter @ikisci/app build`` writes the app to ``static/app/``: its
modules under ``assets/``, named by content hash, and ``manifest.json``, which
names the entry module and the chunks it imports. Pages load the entry as a
module script from Iki's own origin, so the CSP stays ``script-src 'self'``.

In development, with ``IKI_WEB_DEV_SERVER`` on, a page requested through the
Vite dev server (which marks its requests with ``X-Iki-Web-Dev``) loads the
modules from it instead, for instant reloads.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.staticfiles import finders
from django.http import HttpRequest
from django.templatetags.static import static
from servestatic.middleware import ServeStaticMiddleware as BaseServeStaticMiddleware
from servestatic.storage import CompressedManifestStaticFilesStorage

MANIFEST = "app/manifest.json"
ENTRY = "src/main.tsx"
#: Vite names these by content hash already, and chunks import each other by those names.
ASSETS_PREFIX = "app/assets/"
DEV_HEADER = "X-Iki-Web-Dev"


class AppNotBuilt(Exception):  # noqa: N818 - reads as "the app is not built"
    pass


class StaticFilesStorage(CompressedManifestStaticFilesStorage):
    """Hashes and compresses static files, except the app's, which Vite has hashed.

    Hashing them again would break the imports between chunks, which use
    Vite's names.
    """

    def hashed_name(self, name: str, content: Any = None, filename: str | None = None) -> str:  # noqa: ANN401
        if name.startswith(ASSETS_PREFIX):
            return name
        return super().hashed_name(name, content, filename)


class ServeStaticMiddleware(BaseServeStaticMiddleware):
    """Caches the app's files for ever, like Django's hashed ones.

    servestatic recognizes only Django's naming (``name.0123456789ab.css``),
    and its middleware ignores ``SERVESTATIC_IMMUTABLE_FILE_TEST``.
    """

    def immutable_file_test(self, path: str, url: str) -> bool:
        if url.startswith(f"{self.static_prefix}{ASSETS_PREFIX}"):
            return True
        return super().immutable_file_test(path, url)


def _manifest_path() -> Path | None:
    found = finders.find(MANIFEST)
    if isinstance(found, str):
        return Path(found)
    collected = Path(settings.STATIC_ROOT) / MANIFEST
    return collected if collected.is_file() else None


@cache
def _read_manifest(path: Path, mtime: float) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        manifest: dict[str, Any] = json.load(file)
    return manifest


def manifest() -> dict[str, Any]:
    path = _manifest_path()
    if path is None:
        raise AppNotBuilt(f"{MANIFEST} is missing. Build the app: make web")
    return _read_manifest(path, path.stat().st_mtime)


def _imports(chunks: dict[str, Any], key: str, seen: set[str]) -> list[str]:
    files: list[str] = []
    for name in chunks.get(key, {}).get("imports", []):
        if name in seen:
            continue
        seen.add(name)
        files.append(chunks[name]["file"])
        files += _imports(chunks, name, seen)
    return files


def uses_dev_server(request: HttpRequest) -> bool:
    return settings.IKI_WEB_DEV_SERVER and request.headers.get(DEV_HEADER) == "1"


def page_assets(request: HttpRequest) -> dict[str, list[str]]:
    """The module scripts a page loads, and the chunks worth fetching early."""
    if uses_dev_server(request):
        base = f"{settings.STATIC_URL}app/"
        # The preamble sets up React Fast Refresh; Vite's guide inlines it,
        # but the CSP allows no inline scripts, so it's a file.
        return {
            "scripts": [f"{base}@vite/client", f"{base}dev-preamble.js", f"{base}{ENTRY}"],
            "preloads": [],
        }
    chunks = manifest()
    entry = chunks[ENTRY]
    return {
        "scripts": [static("app/" + entry["file"])],
        "preloads": [static("app/" + f) for f in _imports(chunks, ENTRY, set())],
    }
