"""Which addresses outgoing webhooks may call (guard against server-side request forgery).

* https only, on port 443 or 8443, with no user name or password in the address;
* the host must resolve, and every address it resolves to must be public (not private, loopback, link-local,
  carrier-grade NAT, multicast or reserved; IPv4-mapped IPv6 is checked as IPv4);
* checked when the endpoint is saved and again before every request. The request then goes to the address
  that was checked (the connection is pinned to that IP, with the host name kept for TLS and the Host header),
  so a DNS answer that changes between the check and the connection can't reach an internal service;
* redirects are never followed.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

ALLOWED_PORTS = (443, 8443)
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")

# Tests replace this; the signature matches socket.getaddrinfo.
_getaddrinfo = socket.getaddrinfo


class URLRejected(ValueError):
    """The message says what is wrong and how to fix it."""

    def __init__(self, message: str, permanent: bool = True):
        super().__init__(message)
        self.permanent = permanent


def is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_global and not (addr.is_private or addr.is_loopback or addr.is_link_local
                                        or addr.is_multicast or addr.is_reserved or addr.is_unspecified))


def _shown(host: str, limit: int = 60) -> str:
    """A host name for an error message: a very long one is cut short."""
    return host if len(host) <= limit else host[: limit - 1] + "…"


def check_url(raw: str) -> str:
    """Syntax and host checks without DNS. Returns the cleaned URL."""
    url = (raw or "").strip()
    if not url:
        raise URLRejected("Enter the address ShipMatch should send events to, starting with https://.")
    if len(url) > 2000 or any(c.isspace() or ord(c) < 32 or c == "\\" for c in url):
        raise URLRejected("That address has spaces or characters a web address never has.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise URLRejected("That isn't a valid web address.")
    if parts.scheme.lower() != "https":
        raise URLRejected("The address must start with https:// so events are encrypted on the way.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise URLRejected("The address must not contain a user name or password. Use the signing secret to "
                          "check that events come from ShipMatch.")
    if port not in (None, *ALLOWED_PORTS):
        raise URLRejected("Use the standard https port (443) or 8443.")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise URLRejected("The address is missing a host name.")
    if parts.fragment:
        url = urlunsplit(parts._replace(fragment=""))
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        if not is_public(host):
            raise URLRejected("That address points to a private or local network. Webhooks only go to public "
                              "internet addresses.")
    elif host == "localhost" or host.endswith(BLOCKED_SUFFIXES) or "." not in host:
        raise URLRejected(f"{_shown(host)} is a local name. Webhooks only go to public internet addresses.")
    return url


def resolve_public(host: str, port: int) -> list[str]:
    """Every address the host resolves to; refuses if any of them isn't public."""
    try:
        infos = _getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        raise URLRejected(f"ShipMatch couldn't find {_shown(host)}. Check the address for typos.", permanent=False)
    ips = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise URLRejected(f"ShipMatch couldn't find {_shown(host)}. Check the address for typos.", permanent=False)
    private = [ip for ip in ips if not is_public(ip)]
    if private:
        raise URLRejected(f"{_shown(host)} points to a private or local network address, so ShipMatch won't send to it. "
                          "Webhooks only go to public internet addresses.")
    return ips


@dataclass
class Target:
    url: str          # with the checked IP in place of the host name
    host_header: str
    sni_host: str
    ip: str


def check_and_resolve(raw: str) -> Target:
    """Everything above, then the address to connect to."""
    url = check_url(raw)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port or 443
    ips = [host] if _is_ip(host) else resolve_public(host, port)
    ip = ips[0]
    ip_netloc = f"[{ip}]" if ":" in ip else ip
    if parts.port:
        ip_netloc += f":{parts.port}"
    pinned = urlunsplit((parts.scheme, ip_netloc, parts.path or "/", parts.query, ""))
    host_header = host if not parts.port or parts.port == 443 else f"{host}:{parts.port}"
    return Target(url=pinned, host_header=host_header, sni_host=host, ip=ip)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False
