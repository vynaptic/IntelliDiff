"""SSRF guard: a target is reached on public addresses only, unless it sets allow_private_network.

Host names are checked on every new connection by ``GuardedResolver`` (so DNS rebinding is
refused at connect time); aiohttp never resolves IP-literal hosts, so ``check_literal_host``
covers those before each call. A database target's session goes to an address ``vetted`` answered.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

from aiohttp.abc import AbstractResolver, ResolveResult

from deltasleuth.connectors.base import UnsafeTargetError
from deltasleuth.core.spec.targets import address_rule

HostResolver = Callable[[str, int], Awaitable[list[str]]]


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_forbidden_address(address: str) -> bool:
    """True for any non-global address (loopback, private, link-local, reserved, ...) or multicast; an
    IPv6 address that stands for an IPv4 one is judged as that one (::ffff:127.0.0.1 is loopback)."""
    return address_rule(address) is not None


def refuse_forbidden(addresses: list[str]) -> None:
    if not addresses:
        raise UnsafeTargetError("target host resolved to no addresses")
    if any(is_forbidden_address(a) for a in addresses):
        raise UnsafeTargetError(
            "target host resolves to a loopback, private, link-local or reserved address; "
            "set allow_private_network on the target to permit it"
        )


def check_literal_host(host: str) -> None:
    try:
        address = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return  # a name: checked when it is resolved
    refuse_forbidden([address])


async def vetted(host: str, port: int, allow_private: bool) -> list[str]:
    """The addresses ``host`` resolves to now, each one a database target may be reached on: the caller
    dials one of them itself, so no driver resolves the name again. One refused answer refuses them all."""
    addresses = await system_resolve(host, port)
    for address in addresses:
        rule = address_rule(address)
        if rule == "never":
            raise UnsafeTargetError(
                f"{address} is never a target (link-local, unspecified, multicast or metadata)"
            )
        if rule == "private" and not allow_private:
            raise UnsafeTargetError(
                f"{address} is a private address: set allow_private_network on the target"
            )
    return list(dict.fromkeys(addresses))


class GuardedResolver(AbstractResolver):
    """aiohttp resolver that refuses forbidden answers unless private networks are allowed."""

    def __init__(self, resolve: HostResolver, *, allow_private: bool) -> None:
        self._resolve = resolve
        self._allow_private = allow_private

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        addresses = await self._resolve(host, port)
        if not self._allow_private:
            refuse_forbidden(addresses)
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": socket.AF_INET6 if ":" in address else socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            }
            for address in addresses
        ]

    async def close(self) -> None:
        return None
