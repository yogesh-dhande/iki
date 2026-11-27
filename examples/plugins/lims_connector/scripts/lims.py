"""The example LIMS connector's client for its LIMS's REST API, which both actions use.

The LIMS answers at ``https://<lims_host>/api/v1`` (the deployment's `lims_host`
setting), and takes the token in `LIMS_TOKEN` as a bearer token:

- ``GET /proteins?modified_since=<time>&cursor=<cursor>`` lists the proteins
  added or changed since then, a page at a time: ``{"items": [...], "next_cursor":
  ...}``, each item ``{"id", "name", "sequence", "modified_at"}``;
- ``PATCH /proteins/<id>`` changes one's fields.

Answers that ask it to slow down (429) or that fail on the LIMS's side (5xx) are
retried, with backoff. A real LIMS's API differs: this is the part to change.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import HTTPMessage
from typing import IO, Any

ATTEMPTS = 4
MAX_BYTES = 64 * 1024**2


class LimsError(Exception):
    """Something the run's page should say, as its error."""


def host(run: dict[str, Any]) -> str:
    found = run.get("settings", {}).get("lims_host")
    if not found:
        raise LimsError("Set the plugin's `lims_host` to the LIMS's host name.")
    return str(found)


def record_url(lims_host: str, lims_id: str) -> str:
    """Where people see a record: the same page the `lims_id` field links to."""
    return f"https://{lims_host}/proteins/{urllib.parse.quote(lims_id, safe='')}"


def _origin(url: str) -> tuple[str, str, int]:
    parts = urllib.parse.urlsplit(url)
    port = parts.port
    # Keep the whole authority apart from the port, including any user information.
    host = parts.netloc if port is None else parts.netloc.rsplit(":", 1)[0]
    return parts.scheme, host.lower(), 443 if port is None else port


class _LimsRedirects(urllib.request.HTTPRedirectHandler):
    # urllib counts distinct destinations. Refuse revisits to bound the total, too.
    max_redirections = 5
    max_repeats = 1

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if _origin(req.full_url) != _origin(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, "redirect outside the configured HTTPS origin", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, lims_host: str) -> None:
        token = os.environ.get("LIMS_TOKEN")
        if not token:
            raise LimsError("The deployment hasn't set the `LIMS_TOKEN` secret.")
        self.base = f"https://{lims_host}/api/v1"
        self.opener = urllib.request.build_opener(_LimsRedirects())
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "lab-lims-connector",
        }

    def proteins(self, since: str | None) -> Any:  # noqa: ANN401 - a generator of JSON items
        """Each protein added or changed since `since` (every one when None), oldest first."""
        cursor = None
        while True:
            query = {k: v for k, v in (("modified_since", since), ("cursor", cursor)) if v}
            page = self.call(
                "GET", "/proteins" + (f"?{urllib.parse.urlencode(query)}" if query else "")
            )
            yield from page.get("items", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return

    def update(self, lims_id: str, fields: dict[str, Any]) -> None:
        self.call("PATCH", f"/proteins/{urllib.parse.quote(lims_id, safe='')}", fields)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:  # noqa: ANN401 - JSON
        data = json.dumps(body).encode() if body is not None else None
        headers = {**self.headers, **({"Content-Type": "application/json"} if data else {})}
        for attempt in range(1, ATTEMPTS + 1):
            request = urllib.request.Request(  # noqa: S310 - https, to the host the deployment set
                self.base + path, data=data, method=method, headers=headers
            )
            try:
                with self.opener.open(request, timeout=120) as response:
                    answer = response.read(MAX_BYTES + 1)
            except ValueError:
                raise LimsError("The LIMS returned an invalid URL.") from None
            except urllib.error.HTTPError as error:
                error.close()
                if attempt < ATTEMPTS and (error.code == 429 or error.code >= 500):
                    time.sleep(2**attempt)
                    continue
                if error.code in (401, 403):
                    raise LimsError(
                        f"The LIMS refused the token ({error.code}): check `LIMS_TOKEN`."
                    ) from None
                raise LimsError(f"The LIMS answered {error.code} {error.reason}.") from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < ATTEMPTS:
                    time.sleep(2**attempt)
                    continue
                reason = getattr(error, "reason", error)
                raise LimsError(f"Couldn't reach the LIMS: {reason}.") from None
            if len(answer) > MAX_BYTES:
                raise LimsError("The LIMS's answer is over 64 MiB.")
            try:
                return json.loads(answer) if answer else None
            except ValueError:
                raise LimsError("The LIMS's answer isn't JSON.") from None
        raise AssertionError("unreachable")
