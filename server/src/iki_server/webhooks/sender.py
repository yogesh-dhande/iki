"""Sending one webhook request, safely.

A webhook's URL is chosen by a project Admin, so Iki must not let it reach
what they couldn't (server-side request forgery):

- **Public addresses only.** The host is resolved once, and every address
  must be globally routable: private ranges, loopback, link-local (the
  cloud metadata service), CGNAT and IPv6 forms that embed one of those are
  refused, unless the operator allowed that network
  (``IKI_WEBHOOK_ALLOWED_NETWORKS``) for receivers inside it.
- **No rebinding.** The connection goes to the address that was checked,
  while TLS (SNI and certificate) and the Host header use the real name.
- **HTTPS, verified.** ``http://`` only with ``IKI_WEBHOOK_ALLOW_HTTP``.
  ``IKI_WEBHOOK_CA_FILE`` adds an internal CA.
- **No redirects, bounded time.** A 3xx is a failure. One deadline covers
  every address tried, connecting and reading.
- **Nothing read back.** Only the status code is kept, never the body.
"""

import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from functools import cache
from urllib.parse import urlsplit

import urllib3
from django.conf import settings

TIMEOUT_SECONDS = 15.0
USER_AGENT = "Iki-Webhooks/1"
NAT64 = ipaddress.ip_network("64:ff9b::/96")


class Refused(Exception):  # noqa: N818 - what happened: Iki refused to send
    """Iki won't send to this URL (it isn't allowed, or doesn't resolve)."""


@dataclass(frozen=True)
class Result:
    status_code: int | None
    error: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def _embedded(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address carries (mapped, 6to4, Teredo, NAT64)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    if ip.teredo is not None:
        return list(ip.teredo)
    if ip.ipv4_mapped is not None:
        return [ip.ipv4_mapped]
    if ip.sixtofour is not None:
        return [ip.sixtofour]
    if ip in NAT64:
        return [ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)]
    return []


def allowed(address: str) -> bool:
    """Whether Iki may connect to this address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    networks = settings.IKI_WEBHOOK_ALLOWED_NETWORKS
    if any(ip in network for network in networks if network.version == ip.version):
        return True
    embedded = _embedded(ip)
    if embedded:
        return all(part.is_global for part in embedded)
    return ip.is_global


def check_url(url: str) -> None:
    """Refuses URLs Iki would never send to; resolving waits until sending."""
    parts = urlsplit(url)
    schemes = ("https", "http") if settings.IKI_WEBHOOK_ALLOW_HTTP else ("https",)
    if parts.scheme not in schemes:
        raise Refused(
            "Use an https:// address."
            if parts.scheme == "http"
            else "Use an https:// address, such as https://example.com/hooks/iki."
        )
    if not parts.hostname:
        raise Refused("The address needs a host name.")
    if parts.username or parts.password:
        raise Refused("Leave credentials out of the address. Receivers check the signature.")
    if parts.fragment:
        raise Refused("Leave out the #fragment: it’s never sent.")
    try:
        parts.port  # noqa: B018 - raises for an invalid port
    except ValueError as error:
        raise Refused("The port isn’t a number from 1 to 65535.") from error
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        if parts.hostname.rstrip(".").lower() == "localhost" or parts.hostname.endswith(
            ".localhost"
        ):
            raise Refused("Iki only sends to addresses other systems can reach.") from None
        return
    if not allowed(str(literal)):
        raise Refused("Iki only sends to public addresses, or networks your operator allowed.")


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise Refused(f"{host} doesn’t resolve.") from error
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    refused = [a for a in addresses if not allowed(a)]
    if refused:
        # All or nothing: a mix of public and private answers is an attack.
        raise Refused(f"{host} resolves to an address Iki doesn’t send to ({refused[0]}).")
    if not addresses:
        raise Refused(f"{host} doesn’t resolve.")
    return addresses


@cache
def _ssl_context(ca_file: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    return context


def _describe(error: Exception) -> str:
    reason = getattr(error, "reason", None) or error
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in text:
        return "The receiver’s TLS certificate isn’t trusted."
    # Before timeouts: urllib3's NewConnectionError is also a ConnectTimeoutError.
    if isinstance(reason, ConnectionRefusedError) or "Connection refused" in text:
        return "The receiver refused the connection."
    if isinstance(error, urllib3.exceptions.TimeoutError) or "timed out" in text:
        return f"No answer within {int(TIMEOUT_SECONDS)} seconds."
    return f"Couldn’t connect: {type(reason).__name__}."


def post(url: str, body: bytes, headers: dict[str, str]) -> Result:
    """POSTs ``body``; never raises for the receiver's failures."""
    started = time.monotonic()

    def result(status: int | None, error: str) -> Result:
        return Result(status, error, int((time.monotonic() - started) * 1000))

    try:
        check_url(url)
        parts = urlsplit(url)
        host = parts.hostname or ""
        https = parts.scheme == "https"
        port = parts.port or (443 if https else 80)
        addresses = _resolve(host, port)
    except Refused as refused:
        return result(None, str(refused))

    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    sent_headers = {
        **headers,
        "Host": parts.netloc,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    deadline = started + TIMEOUT_SECONDS
    problem = "Couldn’t connect."
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            problem = f"No answer within {int(TIMEOUT_SECONDS)} seconds."
            break
        timeout = urllib3.Timeout(connect=remaining, read=remaining, total=remaining)
        pool: urllib3.HTTPConnectionPool
        if https:
            pool = urllib3.HTTPSConnectionPool(
                host=address,
                port=port,
                timeout=timeout,
                retries=False,
                maxsize=1,
                ssl_context=_ssl_context(settings.IKI_WEBHOOK_CA_FILE),
                server_hostname=host,
                assert_hostname=host,
            )
        else:
            pool = urllib3.HTTPConnectionPool(
                host=address, port=port, timeout=timeout, retries=False, maxsize=1
            )
        try:
            response = pool.urlopen(
                "POST",
                path,
                body=body,
                headers=sent_headers,
                redirect=False,
                preload_content=False,
                assert_same_host=False,
            )
            status = response.status
            response.release_conn()
            if 300 <= status < 400:
                return result(status, "Iki doesn’t follow redirects. Use the final address.")
            if status >= 300:
                return result(status, f"The receiver answered {status}.")
            return result(status, "")
        except urllib3.exceptions.HTTPError as failure:
            problem = _describe(failure)
        finally:
            pool.close()
    return result(None, problem)
