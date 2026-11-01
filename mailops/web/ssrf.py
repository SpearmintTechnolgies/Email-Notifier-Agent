"""SSRF defence for the mailbox-onboarding IMAP/SMTP connect step.

The onboarding form accepts a hostname + port that the server will connect
to for IMAP (port 993) and SMTP (port 465). An attacker who controls (or
guesses) an admin's web session can abuse those two connects as an
arbitrary TCP + TLS probe into internal networks (`127.0.0.1:22`,
`169.254.169.254:*`, Kubernetes service CIDRs, etc.) and read some bytes
back via error messages. That is a classic SSRF vector.

This module enforces, before any socket is opened:

* The hostname is syntactically a plain DNS name (letters, digits, hyphen,
  dots). IP literals are rejected outright: operators do not need to type
  "192.168.1.5" for a Hostinger box, and refusing literals removes the
  easiest way to reach internal targets.
* The port is in the allowlist (993 IMAPS, 465 SMTPS). STARTTLS (143/587)
  is NOT accepted - matches what the existing Himalaya config supports.
* DNS resolves to a public, routable IPv4/IPv6 address only. Any private,
  loopback, link-local, multicast, broadcast, reserved or unspecified
  range is refused. All resolved addresses must pass; a split-horizon host
  that returns both a public and a private address is refused.
* The resolved address is "pinned" and returned to the caller. The caller
  (the IMAP/SMTP validator) connects to the pinned numeric address, not
  the hostname, so a DNS rebinding attack between validate time and
  connect time cannot slip in a 127.0.0.1 answer.

What this module does NOT try to do:

* Protect against hosts whose public IP is leased to an attacker; that is
  a different problem addressed by TLS certificate verification.
* Guarantee the destination IS a real IMAP/SMTP endpoint; the credentials
  check that.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass

# Allowlist of ports we will ever connect to for validation.
ALLOWED_IMAP_PORTS = frozenset({993})
ALLOWED_SMTP_PORTS = frozenset({465})
ALLOWED_PORTS_BY_KIND = {"imap": ALLOWED_IMAP_PORTS, "smtp": ALLOWED_SMTP_PORTS}

# Reuse the same hostname shape the rest of the pipeline uses.
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


class SSRFBlocked(ValueError):
    """A hostname/port combination was refused on SSRF grounds."""


@dataclass(frozen=True)
class ResolvedTarget:
    hostname: str
    port: int
    kind: str  # "imap" or "smtp"
    ip_addresses: tuple[str, ...]  # all resolved addresses (public only)
    family: int  # the socket family of ``ip_addresses[0]``

    @property
    def primary_ip(self) -> str:
        return self.ip_addresses[0]


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    """Return True only for globally routable public addresses.

    ``ipaddress.ip_address(...).is_global`` already covers private, loopback,
    link-local, multicast, unspecified, reserved and 6to4 cases on Python
    3.9+; we double-check a few edge cases that older stdlibs mishandled
    on IPv6.
    """
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
        return False
    if ip.is_unspecified or ip.is_reserved:
        return False
    if getattr(ip, "is_site_local", False):  # IPv6 fec0::/10 (deprecated)
        return False
    # IPv6: refuse mapped/translated and ULA ranges.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None and not _ip_is_public(ipaddress.ip_address(str(ip.ipv4_mapped))):
            return False
        # Unique Local Addresses fc00::/7
        if int(ip) & 0xFE00_0000_0000_0000_0000_0000_0000_0000 == 0xFC00_0000_0000_0000_0000_0000_0000_0000:
            return False
    return bool(getattr(ip, "is_global", True))


def _resolve(host: str) -> list[tuple[int, str]]:
    """Return [(family, address), ...] for ``host``. Raises on any resolver error."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SSRFBlocked(f"could not resolve {host!r}") from exc
    seen: list[tuple[int, str]] = []
    for family, _socktype, _proto, _canon, sockaddr in infos:
        addr = sockaddr[0]
        if (family, addr) not in seen:
            seen.append((family, addr))
    if not seen:
        raise SSRFBlocked(f"no addresses for {host!r}")
    return seen


def validate_target(
    host: str,
    port: int,
    *,
    kind: str,
    resolver=None,
) -> ResolvedTarget:
    """Validate hostname+port and DNS-pin the result for the IMAP/SMTP connect.

    ``resolver`` is only used in tests. In production it is left ``None``
    and the stdlib getaddrinfo is used.
    """
    if kind not in ALLOWED_PORTS_BY_KIND:
        raise SSRFBlocked(f"unknown target kind {kind!r}")
    host = (host or "").strip().lower()
    if not host or not _HOST_RE.match(host):
        raise SSRFBlocked("host must be a plain DNS name like mail.example.com")
    if _is_ip_literal(host):
        raise SSRFBlocked("IP literal addresses are not allowed")
    if not isinstance(port, int):
        raise SSRFBlocked("port must be an integer")
    if port not in ALLOWED_PORTS_BY_KIND[kind]:
        allowed = sorted(ALLOWED_PORTS_BY_KIND[kind])
        raise SSRFBlocked(
            f"port {port} is not allowed for {kind.upper()}; "
            f"only implicit-TLS port(s) {allowed} are permitted"
        )

    answers = (resolver or _resolve)(host)
    public_addrs: list[tuple[int, str]] = []
    for family, addr in answers:
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise SSRFBlocked(f"{host!r} resolved to a malformed address") from exc
        if not _ip_is_public(ip_obj):
            raise SSRFBlocked(
                f"{host!r} resolves to a non-public address ({addr}); refusing to connect"
            )
        public_addrs.append((family, addr))

    family0, _addr0 = public_addrs[0]
    return ResolvedTarget(
        hostname=host,
        port=port,
        kind=kind,
        ip_addresses=tuple(a for _f, a in public_addrs),
        family=family0,
    )
