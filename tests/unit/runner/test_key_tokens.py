"""A run's keys as key tokens. A run created now records the derivation (``params["key_tokens"]``) and shows
each protected key as a token of a derivation of its own, never the token the same text has as a value of
the key's class; a run created before records none and keeps the value tokens its keys were stored under,
through new passes, rechecks, root causes and a soak's later cycles, rolled again or copied. A key of either
compared again now is a one-shot run's key token, which that run's answer names; and two hash-sample draws of
one population, one either side of the change, still share their keys, each re-hashing to its own selection.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.runner import create_run, sample
from deltasleuth.runner.runs import compare_again, key_view
from deltasleuth.security.tokenise import is_token, key_form, mask_key
from deltasleuth.store import repo
from tests.conftest import TESTER

from .conftest import Harness, ScriptedService, doc, spec_text
from .sampled import KEYS, listed

pytestmark = pytest.mark.db

NAMES = ["Ada", "Grace", "Hal"]  # invented names, keys of class name, as the documents' $.name is
LISTING = {
    "kind": "paginated_endpoint", "target": "svc-a", "request": {"method": "GET", "path": "/list"},
    "pagination": {"kind": "cursor", "itemsPath": "$.items", "cursorPath": "$.next", "cursorParam": "cursor"},
    "keyPath": "$.id",
}  # fmt: skip
SOAK = "concurrency: 2, cycleInterval: 0s, stop: {maxCycles: 2}"


def named(keys: list[str] = NAMES, *, execution: str = "", source: dict[str, Any] | None = None) -> str:
    """The unit spec with keys of class ``name``: ``$.name`` holds the same class, tokenised."""
    text = spec_text(keys, execution=execution)
    for old, new in (
        ("class: business_code", "class: name"),
        ('pattern: "^K[0-9]{3}$"', 'pattern: "^[A-Z][a-z]+$"'),
        ("normalize: [trim, upper]", "normalize: [trim]"),
    ):
        assert old in text
        text = text.replace(old, new)
    if source is None:
        return text
    body = parse_yaml_text(text)
    body["key"]["source"] = source
    return json.dumps(body)


def script(service: ScriptedService, keys: list[str] = NAMES) -> None:
    """Ada reads the same on both sides; Grace's name differs; Hal is missing on side b; any other key
    reads the same."""
    for key in keys:
        service.both(key, doc(key, name=key), doc(key, name=key))
    if "Grace" in keys:
        service.both("Grace", doc("Grace", name="Grace"), doc("Grace", name="Gracie"))
    if "Hal" in keys:
        service.script("a", "Hal", (200, doc("Hal", name="Hal")))
        service.script("b", "Hal", (404, {"error": "NOT_FOUND"}))


async def stored_keys(h: Harness, run_id: str, cycle: int = 1) -> dict[str, str]:
    """The cycle's keys, raw (opened with the run's data key) to masked."""
    async with h.ctx.pool.connection() as conn:
        sealer = await h.ctx.sealer(conn, run_id)
        cur = await conn.execute(
            "SELECT key_masked, key_enc FROM ds.run_keys WHERE run_id = %s AND cycle = %s ORDER BY seq",
            (run_id, cycle),
        )
        rows = await cur.fetchall()
    return {sealer.open("run_key", 0, masked, sealed).decode(): masked for masked, sealed in rows}


async def made_before(h: Harness, run_id: str, value_class: str) -> None:
    """Make a run just created, none of its keys read, one created before key tokens: its params without
    ``key_tokens`` and each key it holds stored, and sealed, as the value token of ``value_class``."""
    async with h.ctx.pool.connection() as conn, conn.transaction():
        sealer = await h.ctx.sealer(conn, run_id)
        cur = await conn.execute(
            "SELECT cycle, seq, key_masked, key_enc FROM ds.run_keys WHERE run_id = %s", (run_id,)
        )
        for cycle, seq, masked, sealed in await cur.fetchall():
            key = sealer.open("run_key", 0, masked, sealed).decode()
            value = h.ctx.tokeniser.token(value_class, key)
            await conn.execute(
                "UPDATE ds.run_keys SET key_masked = %s, key_enc = %s"
                " WHERE (run_id, cycle, seq) = (%s, %s, %s)",
                (value, sealer.seal("run_key", 0, value, key.encode()), run_id, cycle, seq),
            )
        await conn.execute("UPDATE ds.runs SET params = params - 'key_tokens' WHERE run_id = %s", (run_id,))


async def sample_keys(h: Harness, run_id: str) -> set[str]:
    async with h.ctx.pool.connection() as conn:
        return {key for rc in await repo.list_root_causes(conn, run_id) for key in rc["sample_keys"]}


def key_tokens(masked: dict[str, str], value_class: str, h: Harness) -> bool:
    """Whether each key shows as a key token: a token's form written k1, never the value token of its text."""
    return all(
        is_token(shown) and shown.startswith(f"tok:{value_class}:k1:")
        and shown != h.ctx.tokeniser.token(value_class, key)
        for key, shown in masked.items()
    )  # fmt: skip


async def test_a_run_created_now_shows_key_tokens_apart_from_value_tokens(
    harness: Harness, service: ScriptedService
) -> None:
    """The keys a run is given are no oracle: none shows as the token its text has as a value, which the
    run's own documents and findings show for that text in that class."""
    script(service)
    run_id = await create_run(harness.ctx, await harness.spec(named()), "batch", who=TESTER)
    run = await harness.finish(run_id)
    masked, tok = await stored_keys(harness, run_id), harness.ctx.tokeniser
    assert sorted(masked) == sorted(NAMES) and key_tokens(masked, "name", harness)
    assert (run["status"], run["params"]["key_tokens"]) == ("completed", "k1")
    results = await harness.results(run_id)
    assert set(results) == set(masked.values())
    view = await key_view(harness.ctx, run_id, masked["Ada"])
    assert view is not None and view.canonical["a"]["name"] == tok.token("name", "Ada") != masked["Ada"]
    [changed] = results[masked["Grace"]]["findings"]
    assert (changed["path_pattern"], changed["value_a"]) == ("$.name", tok.token("name", "Grace"))
    assert results[masked["Grace"]]["confirmation"] == "rechecked"
    [missing] = results[masked["Hal"]]["findings"]
    assert missing["member_key_tok"] == masked["Hal"]  # a document-level finding's member key is the key
    sampled = await sample_keys(harness, run_id)
    assert sampled and sampled <= set(masked.values())


async def test_a_run_created_before_keeps_its_value_tokens(
    harness: Harness, service: ScriptedService
) -> None:
    """A run created before key tokens saves its new passes, rechecks, findings and root causes under the
    value tokens its keys were stored under: the store refuses a result of any other masked key."""
    script(service)
    run_id = await create_run(harness.ctx, await harness.spec(named()), "batch", who=TESTER)
    await made_before(harness, run_id, "name")
    run = await harness.finish(run_id)
    masked, tok = await stored_keys(harness, run_id), harness.ctx.tokeniser
    assert masked == {key: tok.token("name", key) for key in NAMES}
    assert (run["status"], "key_tokens" in run["params"]) == ("completed", False)
    results = await harness.results(run_id)
    assert set(results) == set(masked.values())
    assert results[masked["Grace"]]["confirmation"] == "rechecked"
    [missing] = results[masked["Hal"]]["findings"]
    assert missing["member_key_tok"] == masked["Hal"]
    sampled = await sample_keys(harness, run_id)
    assert sampled and sampled <= set(masked.values())
    assert await key_view(harness.ctx, run_id, masked["Ada"]) is not None


@pytest.mark.parametrize("before", [True, False], ids=["created-before", "created-now"])
async def test_a_key_compared_again_now_is_a_one_shot_run_s_key_token(
    harness: Harness, service: ScriptedService, before: bool
) -> None:
    """Compare now, by a run's masked key: the key is opened from the run's sealed keys and compared in a
    one-shot run created now, which shows it as a key token and names it in its answer, whatever form the
    run it came from holds it in."""
    script(service)
    run_id = await create_run(harness.ctx, await harness.spec(named()), "batch", who=TESTER)
    if before:
        await made_before(harness, run_id, "name")
    await harness.finish(run_id)
    masked = await stored_keys(harness, run_id)
    again = await compare_again(harness.ctx, run_id, masked["Grace"], who=TESTER)
    assert again is not None
    one_shot = again.result.attrs["run_id"]
    shown = await stored_keys(harness, one_shot)
    assert key_tokens(shown, "name", harness) and list(shown) == ["Grace"]
    assert (shown["Grace"] == masked["Grace"]) is not before  # one derivation, one token
    assert again.result.attrs["key_masked"] == shown["Grace"]
    assert await key_view(harness.ctx, one_shot, shown["Grace"]) is not None


@pytest.mark.parametrize(
    ("before", "rolling"),
    [(True, True), (False, True), (True, False)],
    ids=["created-before-rolling", "created-now-rolling", "created-before-copied"],
)
async def test_a_soak_keeps_its_form_in_its_later_cycles(
    harness: Harness, service: ScriptedService, before: bool, rolling: bool
) -> None:
    """A soak's form is its creation's: a rolling soak's keys listed again, and a copied soak's keys, are
    masked and saved in it in every later cycle."""
    script(service, ["Ada", "Grace", "Hal"])
    service.listings = [["Ada", "Grace"], ["Ada", "Hal"]]  # a key leaves, another comes
    spec_id = await harness.spec(named(execution=SOAK, source=LISTING))
    run_id = await create_run(harness.ctx, spec_id, "soak", soak={"rolling": rolling}, who=TESTER)
    if before:
        await made_before(harness, run_id, "name")
    assert (await harness.finish(run_id))["cycle"] == 2
    tok = harness.ctx.tokeniser
    for cycle, listed_keys in ((1, ["Ada", "Grace"]), (2, ["Ada", "Hal"] if rolling else ["Ada", "Grace"])):
        masked = await stored_keys(harness, run_id, cycle)
        assert sorted(masked) == listed_keys
        if before:
            assert masked == {key: tok.token("name", key) for key in listed_keys}
        else:
            assert key_tokens(masked, "name", harness)
        assert set(await harness.results(run_id, cycle)) == set(masked.values())


async def test_draws_either_side_of_the_change_share_their_keys(
    harness: Harness, service: ScriptedService
) -> None:
    """Two bottom_k draws of one population, the first by a run created before key tokens: a key's u is
    hashed from its raw text, so both draw the whole frame; each stores its keys in its own run's form, and
    each draw's keys re-hash to its selection (R-d), so the earlier draw's attests hold. Their masked keys are
    never compared as stored: recomputed from the earlier run's sealed raw keys in the later run's form, they
    are the later draw's keys."""
    body = json.loads(listed())
    body["key"]["class"] = "identifier"  # a protected key: the unit spec's is a business code
    for key in KEYS:
        service.both(key, doc(key), doc(key))
    service.listings = [KEYS]
    spec_id = await harness.spec(json.dumps(body))
    first = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
    await made_before(harness, first, "identifier")  # no key yet: its draw comes later
    await harness.finish(first)
    second = await create_run(harness.ctx, spec_id, "batch", who=TESTER)
    await harness.finish(second)
    earlier, later = await stored_keys(harness, first), await stored_keys(harness, second)
    tok = harness.ctx.tokeniser
    assert sorted(earlier) == sorted(later) == KEYS  # opened, the draws overlap whole
    assert earlier == {key: tok.token("identifier", key) for key in KEYS}
    assert key_tokens(later, "identifier", harness)
    assert not set(earlier.values()) & set(later.values())
    spec = compile_spec(body, targets=harness.ctx.targets)
    async with harness.ctx.pool.connection() as conn:
        runs = [await repo.get_run(conn, run_id) for run_id in (first, second)]
        assert [(run or {}).get("params", {}).get("key_tokens") for run in runs] == [None, "k1"]
        for run_id in (first, second):
            drawn = await repo.get_draw(conn, run_id, 1)
            assert drawn is not None and await sample.reselected(conn, drawn)
    form = key_form(runs[1]["params"])  # type: ignore[index]
    assert {mask_key(key, spec, tok, form=form) for key in earlier} == set(later.values())
