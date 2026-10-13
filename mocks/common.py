"""Shared machinery for the mock services: reply scripts, datasets and the app skeleton.

A variant answers each key from a *reply script*: the replies it gives on the first, second, ...
call for that key, the last one repeating forever. Time-dependent seeds (a stale first read, a
5xx before success) are therefore plain data, the apps stay trivial, and the fixture writer can
record exactly what a polite client would have kept.

Every app counts calls per key, because the scripts are indexed by that count, and exposes three
housekeeping routes that no comparison ever calls:

* ``GET /__health``: liveness plus the pair and variant it serves;
* ``GET /__calls``: calls per key so far (tests assert retries and rechecks with it);
* ``POST /__reset``: forget the counts, so a fresh run sees first-call behaviour again.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from aiohttp import web

Variant = Literal["a", "b"]
VARIANTS: tuple[Variant, ...] = ("a", "b")

JSONBody = dict[str, Any]
"""A response body as plain Python JSON (numbers are int or float: what a real service emits)."""

JSON_CONTENT_TYPE = "application/json"


@dataclass(frozen=True)
class Reply:
    """One HTTP answer: a status and a JSON body."""

    status: int
    body: JSONBody


Script = tuple[Reply, ...]
"""Replies for calls 1, 2, ... of one key; the last reply repeats for every later call."""


def not_found_reply() -> Reply:
    """The documented not-found answer, which both targets declare authoritative.

    A fresh body per call: rendered bodies are never shared between scripts.
    """
    return Reply(404, {"error": "NOT_FOUND"})


@dataclass(frozen=True)
class ScriptedDataset:
    """What both variants of one pair answer, for every key they know.

    ``seeded`` names each seeded discrepancy and the key that carries it; expected.json lists the
    same keys. A key missing from a variant's scripts gets the documented not-found answer.
    """

    pair: str
    rng_seed: int
    keys: tuple[str, ...]  # the sample in keys.txt, in file order
    seeded: Mapping[str, str]  # discrepancy name -> key
    scripts: Mapping[Variant, Mapping[str, Script]]

    def script(self, variant: Variant, key: str) -> Script:
        return self.scripts[variant].get(key) or (not_found_reply(),)

    def reply(self, variant: Variant, key: str, call_no: int) -> Reply:
        """The answer to the ``call_no``-th call (1-based) for ``key``."""
        if call_no < 1:
            raise ValueError(f"call numbers start at 1, got {call_no}")
        script = self.script(variant, key)
        return script[min(call_no, len(script)) - 1]


def encode_body(body: JSONBody) -> bytes:
    """Compact UTF-8 JSON.

    The apps and the fixture writer share this one encoder, so a recorded sample is byte-for-byte
    the body the app serves.
    """
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def json_response(status: int, body: JSONBody) -> web.Response:
    return web.Response(status=status, body=encode_body(body), content_type=JSON_CONTENT_TYPE)


DATASET = web.AppKey("dataset", ScriptedDataset)
VARIANT = web.AppKey("variant", str)
CALLS = web.AppKey("calls", Counter)


def build_app(dataset: ScriptedDataset, variant: Variant, route: str) -> web.Application:
    """An app serving ``variant`` of ``dataset`` at ``route`` (which holds a ``{key}`` segment).

    HEAD is not routed: an automatic HEAD would advance the per-key call count and silently
    consume a scripted first reply.
    """
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
    if "{key}" not in route:
        raise ValueError(f"route must contain a {{key}} segment, got {route!r}")
    app = web.Application()
    app[DATASET] = dataset
    app[VARIANT] = variant
    app[CALLS] = Counter()
    app.router.add_get(route, _serve_key, allow_head=False)
    app.router.add_get("/__health", _health, allow_head=False)
    app.router.add_get("/__calls", _calls, allow_head=False)
    app.router.add_post("/__reset", _reset)
    return app


async def _serve_key(request: web.Request) -> web.Response:
    app = request.app
    key = request.match_info["key"]
    # No await between the increment and the read: calls for one key are numbered exactly even
    # when a client fires them concurrently.
    app[CALLS][key] += 1
    reply = app[DATASET].reply(app[VARIANT], key, app[CALLS][key])
    return json_response(reply.status, reply.body)


async def _health(request: web.Request) -> web.Response:
    app = request.app
    return json_response(200, {"status": "ok", "pair": app[DATASET].pair, "variant": app[VARIANT]})


async def _calls(request: web.Request) -> web.Response:
    return json_response(200, {"calls": dict(sorted(request.app[CALLS].items()))})


async def _reset(request: web.Request) -> web.Response:
    request.app[CALLS].clear()
    return json_response(200, {"status": "reset"})
