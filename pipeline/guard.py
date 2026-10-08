"""Checks on paths and URLs that come from documents or agents.

A file name in a packet, a `file` column in the register and a `url` on a
fetched row are all data someone else chose. Before code opens one, it checks
that a path stays inside the packet and that a URL is a public http(s) address,
so a crafted name cannot read outside the job and a crafted link cannot reach
the host's own network (architecture p.12: document content is data, never
instructions).
"""
from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit


class UnsafeInput(ValueError):
    pass


def inside(root: Path | str, rel: str) -> Path:
    """`root / rel`, refused if it resolves (symlinks followed) outside `root`."""
    base = Path(root).resolve()
    path = (base / rel).resolve()
    if path != base and base not in path.parents:
        raise UnsafeInput(f"{rel!r} resolves outside {base}")
    return path


def public_url(url: str, *, resolve: bool = True) -> str:
    """The URL, if it is http(s) to a host that resolves only to public addresses.

    Refuses file:, ftp: and other schemes, credentials in the URL, and any host
    on a loopback, private, link-local (cloud metadata) or reserved address.
    With `resolve=False` the host is checked as written (a literal IP or
    localhost) without a DNS lookup; the fetch itself resolves and re-checks.
    """
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https"):
        raise UnsafeInput(f"only http(s) URLs are fetched, not {parts.scheme or 'no scheme'!r}")
    if parts.username or parts.password:
        raise UnsafeInput("a URL carrying credentials is not fetched")
    host = parts.hostname
    if not host:
        raise UnsafeInput("URL has no host")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeInput(f"{host} is the local machine")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise UnsafeInput(f"{host} is a non-public address")
    if not resolve:
        return url
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError) as e:
        raise UnsafeInput(f"{host} does not resolve ({e})") from None
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise UnsafeInput(f"{host} resolves to non-public address {addr}")
    return url
