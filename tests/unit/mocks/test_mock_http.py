"""The mock services over HTTP: every scripted reply, and each seeded behaviour, as a client sees it.

Requests are built from each spec's own request template (path, query, headers), so these tests
also prove that the specs address the routes the apps serve.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp import ClientResponse

from mocks.common import JSON_CONTENT_TYPE, ScriptedDataset, Variant, encode_body
from mocks.customer.dataset import SEEDED_KEYS as CUSTOMER_KEYS
from mocks.customer.dataset import Seed as CustomerSeed
from mocks.linker.dataset import SEEDED_KEYS as LINKER_KEYS
from mocks.linker.dataset import Seed as LinkerSeed
from mocks.registry import PAIRS

Serve = Callable[..., Any]


def request_args(spec: dict[str, Any], variant: Variant, key: str) -> dict[str, Any]:
    """URL path, repeated query params and headers exactly as the spec's template renders them."""
    template = spec["sides"][variant]["request"]
    assert template["method"] == "GET"
    params = [
        (name, str(v))
        for name, value in template.get("query", {}).items()
        for v in (value if isinstance(value, list) else [value])
    ]
    return {
        "path": template["path"].replace("{{ key }}", quote(key, safe="")),
        "params": params,
        "headers": template.get("headers", {}),
    }


async def fetch(client: Any, spec: dict[str, Any], variant: Variant, key: str) -> ClientResponse:
    args = request_args(spec, variant, key)
    return await client.get(args["path"], params=args["params"], headers=args["headers"])


@pytest.mark.parametrize("variant", ["a", "b"])
@pytest.mark.parametrize("pair", PAIRS)
async def test_every_key_serves_its_script_byte_for_byte(
    serve: Serve, specs: dict[str, Any], datasets: dict[str, ScriptedDataset], pair: str, variant: Variant
) -> None:
    ds = datasets[pair]
    client = await serve(pair, variant, ds)
    for key in ds.keys:
        script = ds.script(variant, key)
        for call_no in range(1, len(script) + 2):  # one call past the script: the last reply repeats
            resp = await fetch(client, specs[pair], variant, key)
            want = ds.reply(variant, key, call_no)
            assert resp.status == want.status, (key, call_no)
            assert resp.content_type == JSON_CONTENT_TYPE
            assert await resp.read() == encode_body(want.body), (key, call_no)


@pytest.mark.parametrize("pair", PAIRS)
async def test_other_variants_route_is_unknown(serve: Serve, specs: dict[str, Any], pair: str) -> None:
    ds_key = specs[pair]["key"]["source"]["keys"][0]
    client_a = await serve(pair, "a")
    resp = await client_a.get(request_args(specs[pair], "b", ds_key)["path"])
    assert resp.status == 404 and resp.content_type != JSON_CONTENT_TYPE  # a plain router 404


async def test_linker_drift_stale_first_then_current_and_reset_restores(
    serve: Serve, specs: dict[str, Any]
) -> None:
    key = LINKER_KEYS[LinkerSeed.DRIFT]
    client = await serve("linker", "b")
    names = [
        (await (await fetch(client, specs["linker"], "b", key)).json())["data"]["entityName"]
        for _ in range(3)
    ]
    assert names[0] != names[1] == names[2]
    assert (await (await client.get("/__calls")).json()) == {"calls": {key: 3}}
    await client.post("/__reset")
    again = (await (await fetch(client, specs["linker"], "b", key)).json())["data"]["entityName"]
    assert again == names[0]


async def test_linker_server_error_then_success(serve: Serve, specs: dict[str, Any]) -> None:
    key = LINKER_KEYS[LinkerSeed.SERVER_ERROR_THEN_SUCCESS]
    client = await serve("linker", "b")
    first = await fetch(client, specs["linker"], "b", key)
    assert first.status == 503 and await first.json() == {"error": "UNAVAILABLE"}
    for _ in range(2):
        assert (await fetch(client, specs["linker"], "b", key)).status == 200


async def test_linker_truncation_over_http(serve: Serve, specs: dict[str, Any]) -> None:
    key = LINKER_KEYS[LinkerSeed.TRUNCATION]
    a = await (await fetch(await serve("linker", "a"), specs["linker"], "a", key)).json()
    b = await (await fetch(await serve("linker", "b"), specs["linker"], "b", key)).json()
    assert (len(a["links"]), a["linkCount"]) == (57, 57)
    assert (len(b["data"]["linkedEntities"]), b["data"]["totalLinked"]) == (50, 57)


@pytest.mark.parametrize(
    ("pair", "variant", "key", "status", "body"),
    [
        ("linker", "b", LINKER_KEYS[LinkerSeed.ONE_SIDED_404], 404, {"error": "NOT_FOUND"}),
        ("linker", "a", LINKER_KEYS[LinkerSeed.UNANNOUNCED_404], 404, {"message": "no route"}),
        ("linker", "a", LINKER_KEYS[LinkerSeed.ABSENT_BOTH], 404, {"error": "NOT_FOUND"}),
        ("linker", "b", LINKER_KEYS[LinkerSeed.ABSENT_BOTH], 404, {"error": "NOT_FOUND"}),
        ("customer", "b", CUSTOMER_KEYS[CustomerSeed.ONE_SIDED_404], 404, {"error": "NOT_FOUND"}),
    ],
)
async def test_not_found_answers(
    serve: Serve,
    specs: dict[str, Any],
    pair: str,
    variant: Variant,
    key: str,
    status: int,
    body: dict[str, Any],
) -> None:
    resp = await fetch(await serve(pair, variant), specs[pair], variant, key)
    assert resp.status == status and resp.content_type == JSON_CONTENT_TYPE
    assert await resp.json() == body


@pytest.mark.parametrize(
    ("pair", "variant", "key"),
    [
        ("linker", "a", LINKER_KEYS[LinkerSeed.ONE_SIDED_404]),
        ("linker", "b", LINKER_KEYS[LinkerSeed.UNANNOUNCED_404]),
        ("customer", "a", CUSTOMER_KEYS[CustomerSeed.ONE_SIDED_404]),
    ],
)
async def test_the_other_side_of_a_one_sided_404_answers(
    serve: Serve, specs: dict[str, Any], pair: str, variant: Variant, key: str
) -> None:
    assert (await fetch(await serve(pair, variant), specs[pair], variant, key)).status == 200


async def test_customer_seeds_over_http(serve: Serve, specs: dict[str, Any]) -> None:
    a_client, b_client = await serve("customer", "a"), await serve("customer", "b")

    async def both(seed: CustomerSeed) -> tuple[dict[str, Any], dict[str, Any]]:
        key = CUSTOMER_KEYS[seed]
        a = await (await fetch(a_client, specs["customer"], "a", key)).json()
        b = await (await fetch(b_client, specs["customer"], "b", key)).json()
        return a["customer"], b["data"]

    a, b = await both(CustomerSeed.CONSENT_GRANTED_MISMATCH)
    assert (a["consents"][0]["granted"], b["consents"][0]["status"]) == ("Y", "WITHDRAWN")
    a, b = await both(CustomerSeed.ACCOUNT_MISSING_IN_V2)
    assert (a["accountCount"], b["productCount"]) == (2, 1)
    a, b = await both(CustomerSeed.TAGS_CASE_ONLY)
    assert (sorted(a["tags"]), sorted(b["labels"])) == (["PAPERLESS", "VIP"], ["paperless", "vip"])
    a, b = await both(CustomerSeed.PHONE_FORMAT_ONLY)
    assert a["phones"][0]["number"].startswith("07700 ")
    assert next(p["value"] for p in b["contactPoints"] if p["channel"] == "PHONE").startswith("+447700")
    a, b = await both(CustomerSeed.BALANCE_SCALE_ONLY)
    assert (a["accounts"][0]["balance"], b["products"][0]["balance"]["amount"]) == ("1250.10", 1250.1)
    a, b = await both(CustomerSeed.CREATED_AT_MS_ONLY)
    assert b["createdAt"] == a["createdAt"].removesuffix("Z") + ".000Z"
    a, b = await both(CustomerSeed.SEGMENT_KNOWN_DIFF)
    assert (a["segment"], b["segment"]) == ("PRIVATE", "WEALTH")
    a, b = await both(CustomerSeed.SMS_PREFERENCE_MISSING)
    assert a["prefs"]["mktSms"] == "N" and "sms" not in b["preferences"]["channels"]


async def test_customer_ignores_query_parameters(serve: Serve) -> None:
    client = await serve("customer", "b")
    key = CUSTOMER_KEYS[CustomerSeed.SEGMENT_KNOWN_DIFF]
    bare = await (await client.get(f"/v2/customers/{key}")).read()
    with_query = await (
        await client.get(f"/v2/customers/{key}", params=[("include", "x"), ("zzz", "1")])
    ).read()
    assert bare == with_query


async def test_health_per_pair_and_variant(serve: Serve) -> None:
    for pair in PAIRS:
        for variant in ("a", "b"):
            client = await serve(pair, variant)
            assert await (await client.get("/__health")).json() == {
                "status": "ok",
                "pair": pair,
                "variant": variant,
            }
