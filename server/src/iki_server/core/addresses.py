"""Which network addresses Iki may connect to on someone else's say-so.

Webhook receivers (chosen by a project Admin) and the hosts a plugin's action
reaches (through the egress proxy) must not reach what lies behind the
deployment's firewall (server-side request forgery). Only globally routable
addresses qualify: private ranges, loopback, link-local (the cloud metadata
service), CGNAT and IPv6 forms that embed one of those are refused, unless the
operator allowed a network for systems inside it.
"""

import ipaddress
from collections.abc import Iterable

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

NAT64 = ipaddress.ip_network("64:ff9b::/96")


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


def allowed(address: str, networks: Iterable[Network] = ()) -> bool:
    """Whether Iki may connect to this address: a public one, or one in `networks`."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if any(ip in network for network in networks if network.version == ip.version):
        return True
    embedded = _embedded(ip)
    if embedded:
        return all(part.is_global for part in embedded)
    return ip.is_global
