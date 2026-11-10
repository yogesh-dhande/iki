"""Which redirect URIs a client may use.

Redirects must be https, with one exception from RFC 8252 §7.3: a native app
on the person's own computer (a CLI, a desktop app) listens on a loopback
address over plain http, since nothing leaves the machine. Plain http to any
other host would hand the authorization code to the network, so it is refused
when a client registers and again whenever it asks for a code.
"""

from collections.abc import Iterable
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_allowed(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and host in LOOPBACK_HOSTS


def refused(uris: Iterable[str]) -> list[str]:
    """The URIs among ``uris`` that a client may not redirect to."""
    return [uri for uri in uris if not is_allowed(uri)]
