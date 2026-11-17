"""What the small_molecule plugin's fetches share: asking a public REST API for JSON, with
retries, and the identifiers people give them.

An answer that asks to slow down (429), or that fails on the service's side (5xx, such as
PubChem's 503 when it's busy), is asked again, waiting longer each time. One that says the
record isn't there (404) is `NotFound`. The run reaches only the hosts its action names,
through the deployment's egress proxy, which `urllib` finds in HTTPS_PROXY.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from chemistry import UnreadableError

ATTEMPTS = 4
MAX_BYTES = 32 * 1024**2
#: A full InChIKey, standard or not.
ANY_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
MAX_IDENTIFIERS = 100


class NotFound(Exception):  # noqa: N818 - reads as what happened
    """The service has no such record."""


def get_json(
    url: str, service: str, agent: str, form: dict[str, str] | None = None
) -> tuple[Any, bytes]:
    """The JSON at `url`, and the bytes it was read from; with `form`, posted to it."""
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    for attempt in range(1, ATTEMPTS + 1):
        request = urllib.request.Request(  # noqa: S310 - a fixed https URL
            url, data=data, headers={"User-Agent": agent, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                body = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            code, reason = error.code, error.reason
            error.close()
            if code == 404:
                raise NotFound(url) from None
            if attempt < ATTEMPTS and (code == 429 or code >= 500):
                time.sleep(2**attempt)
                continue
            raise UnreadableError(f"{service} answered {code} {reason}") from None
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < ATTEMPTS:
                time.sleep(2**attempt)
                continue
            why = getattr(error, "reason", error)
            raise UnreadableError(f"Couldn't reach {service}: {why}") from None
        if len(body) > MAX_BYTES:
            raise UnreadableError(f"{service}'s answer is over 32 MiB")
        try:
            return json.loads(body), body
        except ValueError:
            raise UnreadableError(f"{service}'s answer isn't JSON") from None
    raise AssertionError("unreachable")


def identifiers(text: str, example: str) -> list[str]:
    """What to fetch, one a line, in order, each once."""
    found = list(dict.fromkeys(" ".join(line.split()) for line in text.splitlines()))
    found = [f for f in found if f and not f.startswith("#")]
    if not found:
        raise UnreadableError(f"Give at least one, such as {example}.")
    if len(found) > MAX_IDENTIFIERS:
        raise UnreadableError(
            f"That's {len(found):,}, more than the {MAX_IDENTIFIERS} one run fetches: split them."
        )
    return found
