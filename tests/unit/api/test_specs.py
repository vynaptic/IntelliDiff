"""Specs: upload, validation with issue pointers, reading back, the builder's form (joined into its
document and split again), one side's request tried against its endpoint (masked, or as it came in a
trial), and drafting from sample pairs."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from fastapi.testclient import TestClient

from deltasleuth import targets as product
from deltasleuth.api.routes import specs as routes
from deltasleuth.cli import main as cli
from deltasleuth.connectors.files import FileSampleSource
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.spec.documents import parse_yaml_text
from deltasleuth.core.types import Side
from deltasleuth.profiler import bootstrap
from deltasleuth.runner.keys import normalise_keys, run_keys
from deltasleuth.security import tokenise
from mocks import fixtures
from mocks.registry import PAIRS

from .conftest import RAW_KEY, TARGETS, audited, said

pytestmark = pytest.mark.db
OpenClient = Callable[..., AbstractContextManager[TestClient]]
ABS = "/compare/fields/0/compare/tolerance/abs"
TRIAL = {"public_url": "http://127.0.0.1:8000"}  # tokens mode on a plain-http loopback address
GET = {"method": "GET", "path": "/items/{{ key }}"}
PRICE = "1.00000000000000000001"  # more digits than a float holds
STAND_IN = r"tok:free_(text|number):v1:[A-Z2-7]{26}"
# A host the targets' rules read as a name and every resolver, without a lookup, as 127.0.0.1: what a name
# that resolves to a private address is to the guard (host.docker.internal without its consent).
GUARDED = "0x7f.1"


def test_a_spec_is_stored_once_per_content_and_read_back(
    client: TestClient, spec_yaml: str, audit: Any
) -> None:
    created = client.post("/v1/specs", content=spec_yaml, headers={"content-type": "application/yaml"})
    assert created.status_code == 201
    spec_id = created.json()["spec_id"]
    # What gates its runs under SSO, and its approval:
    assert {name: created.json()[name] for name in ("needs", "approval_id")} == {
        "needs": ["clear"],
        "approval_id": None,
    }
    assert client.post("/v1/specs", json=yaml.safe_load(spec_yaml)).json()["spec_id"] == spec_id  # JSON
    assert [(s["spec_id"], s["approval"]) for s in client.get("/v1/specs").json()["items"]] == [
        (spec_id, None)
    ]
    assert client.get(f"/v1/specs/{spec_id}").json()["body"]["metadata"]["name"] == "api-demo"
    assert client.get("/v1/specs/999").status_code == 404
    assert audit(client) == ["spec.create", "spec.create"]


def test_spec_detail_masks_inline_identifier_keys(client: TestClient, spec_yaml: str) -> None:
    spec_id = client.post("/v1/specs", content=spec_yaml).json()["spec_id"]
    shown = client.get(f"/v1/specs/{spec_id}")
    assert "K12345678" not in shown.text  # key.class identifier: tokenised, as in every run view
    assert shown.json()["body"]["key"]["source"]["keys"][0].startswith("tok:")


def test_spec_yaml(client: TestClient, spec_yaml: str) -> None:
    """A stored spec reads back as YAML rendered from its stored document under two comment lines (its
    name, version, id and content hash; that it is the parsed document, not the upload's text), its inline
    keys masked, with who stored it by name: none before identities were recorded. Keys of class
    ``business_code``, the default, are shown in clear, and the second line says so."""
    spec_id = client.post("/v1/specs", content=spec_yaml).json()["spec_id"]
    shown = client.get(f"/v1/specs/{spec_id}").json()
    head, _, rest = shown["yaml"].partition("\n\n")
    assert " ".join(line.removeprefix("#").strip() for line in head.splitlines()) == (
        f"api-demo 1.0.0 (spec {spec_id}), content hash {shown['content_hash']}"
        " As stored, parsed: the upload's comments and key order are not kept; inline keys are masked."
    )
    assert all(line.startswith("# ") for line in head.splitlines()) and "#" not in rest
    assert yaml.safe_load(shown["yaml"]) == shown["body"]  # the masked body and nothing else
    assert "K12345678" not in shown["yaml"] and "keys: ['tok:identifier:" in rest  # masked, as the body's
    assert "&side" not in rest and "<<" not in rest  # parsed: neither an anchor nor a merge key is kept
    assert (shown["created_by"], shown["created_by_name"]) == ("dev:loopback", "dev:loopback")
    earlier = spec_yaml.replace('"1.0.0"', '"0.9.0"')  # another version, as one stored before 005
    early = client.portal.call(_edited, client.app.state.ctx, earlier, None)
    shown = client.get(f"/v1/specs/{early}").json()
    assert (early != spec_id, shown["created_by"], shown["created_by_name"]) == (True, None, None)
    assert f"# api-demo 0.9.0 (spec {early}), content hash" in shown["yaml"]
    assert "owner: someone else" in shown["yaml"]  # the stored document, whatever was uploaded
    doc = yaml.safe_load(spec_yaml)
    doc["key"] |= {"class": "business_code", "source": {"kind": "list", "keys": ["K12345678", "K87654321"]}}
    for version in ("1.1.0", "1.2.0"):  # the class declared, then left out: it is the default
        doc["metadata"]["version"] = version
        shown = client.get(f"/v1/specs/{client.post('/v1/specs', json=doc).json()['spec_id']}").json()
        head, _, rest = shown["yaml"].partition("\n\n")
        assert " ".join(line.removeprefix("#").strip() for line in head.splitlines()).endswith(
            " As stored, parsed: the upload's comments and key order are not kept; inline keys are in clear"
            " (key.class business_code)."
        )
        assert "keys: [K12345678, K87654321]" in rest and "masked" not in head
        doc["key"].pop("class", None)


def test_masked_inline_composite_keys(client: TestClient, spec_yaml: str) -> None:
    """A multi-part key's inline keys are shown as a run of the spec shows them: each part normalised, then
    one token of ``key.class`` for the joined key."""
    doc = yaml.safe_load(spec_yaml)
    listed = [" b12 | k12345678 ", "B12|K87654321", "b12|K12345678"]
    doc["key"] |= {
        "type": "composite", "parts": ["branch", "item"], "pattern": "^(B[0-9]{2}|K[0-9]{8})$",
        "normalize": ["trim", "upper"], "source": {"kind": "list", "keys": listed},
    }  # fmt: skip
    shown = client.get(f"/v1/specs/{client.post('/v1/specs', json=doc).json()['spec_id']}")
    keys = shown.json()["body"]["key"]["source"]["keys"]
    ctx = client.app.state.ctx
    spec = compile_spec(doc, targets=ctx.targets)
    taken = run_keys(normalise_keys(listed, spec), spec, ctx.tokeniser)
    assert list(taken) == ["B12|K12345678", "B12|K87654321"]  # the third repeats the first
    assert keys == [taken["B12|K12345678"], taken["B12|K87654321"], taken["B12|K12345678"]]
    assert all(key.startswith("tok:identifier:") and "|" not in key for key in keys)
    assert not any(raw in shown.text.upper() for raw in ("B12", "K12345678", "K87654321"))


def test_a_body_the_store_cannot_keep_as_uploaded_is_refused(client: TestClient, spec_yaml: str) -> None:
    # jsonb holds neither NUL nor -0.0, so the stored body would not re-hash.
    doc = yaml.safe_load(spec_yaml)
    nul = doc | {"metadata": doc["metadata"] | {"owner": "te\x00sts"}}
    tolerance = {"fields": [{"path": "$.price", "compare": {"tolerance": {"abs": -0.0}}}]}
    replies = [client.post("/v1/specs", json=body) for body in (nul, doc | {"compare": tolerance})]
    assert [(r.status_code, r.json()["detail"]) for r in replies] == [
        (422, "spec body holds NUL at /metadata/owner, which the store cannot keep"),
        (422, f"spec body holds -0.0 at {ABS}, which the store keeps as 0: write 0"),
    ]
    assert client.get("/v1/specs").json()["items"] == []


def test_a_body_that_does_not_come_back_from_the_store_as_uploaded_is_refused(
    client: TestClient, spec_yaml: str, audit: Any
) -> None:
    # jsonb keeps 1e16 as the integer 10000000000000000, whose text the content hash would not match.
    doc = yaml.safe_load(spec_yaml)
    tolerance = {"fields": [{"path": "$.price", "compare": {"tolerance": {"abs": 1.0e16}}}]}
    huge = client.post("/v1/specs", json=doc | {"compare": tolerance})
    edited = client.portal.call(_edited, client.app.state.ctx, spec_yaml)
    again = client.post("/v1/specs", content=spec_yaml)  # the same content maps to the edited row
    integer = 10**16
    assert (huge.status_code, huge.json()["detail"]) == (
        422, f"spec body holds 1e+16 at {ABS}, which the store keeps as {integer}: write {integer}",
    )  # fmt: skip
    assert (again.status_code, again.json()["detail"]) == (
        422,
        f"spec {edited}'s stored body differs from this upload at /metadata/owner (edited, or not storable"
        " as uploaded): upload a changed version (a new version)",
    )
    assert [s["spec_id"] for s in client.get("/v1/specs").json()["items"]] == [edited]
    assert audit(client) == []  # both uploads rolled back


async def _edited(ctx: Any, text: str, created_by: str | None = "local:ops@host") -> int:
    """The spec stored with its owner edited after its content hash was taken; its spec_id."""
    from deltasleuth.core.spec import compile_spec
    from deltasleuth.core.spec.documents import parse_yaml_text
    from deltasleuth.store import repo

    spec = compile_spec(parse_yaml_text(text), targets=ctx.targets)
    raw = spec.raw | {"metadata": spec.raw["metadata"] | {"owner": "someone else"}}
    async with ctx.pool.connection() as conn:
        return await repo.save_spec(conn, dataclasses.replace(spec, raw=raw), created_by=created_by)


def test_an_invalid_spec_is_refused_with_issue_pointers(client: TestClient, spec_yaml: str) -> None:
    unknown_target = client.post("/v1/specs", content=spec_yaml.replace("target: svc-b", "target: svc-z"))
    assert (unknown_target.status_code, unknown_target.json()["detail"]) == (422, "invalid spec")
    assert "/sides/b/target" in {issue["pointer"] for issue in unknown_target.json()["issues"]}
    broken = client.post("/v1/specs", content="key: [1,")
    assert (broken.status_code, broken.json()["issues"][0]["code"]) == (422, "yaml_syntax")
    assert client.get("/v1/specs").json()["items"] == []


def test_validate_lists_issues_without_storing(client: TestClient, spec_yaml: str) -> None:
    validated = client.post("/v1/specs/validate", content=spec_yaml).json()
    assert (validated["valid"], [issue["code"] for issue in validated["issues"]]) == (
        True,
        ["inline_keys_stored"],  # a warning: identifier keys kept in clear with the spec
    )
    broken = client.post("/v1/specs/validate", content="key: [1,").json()
    assert (broken["valid"], broken["issues"][0]["code"]) == (False, "yaml_syntax")
    assert client.get("/v1/specs").json()["items"] == []
    # Identifier keys that no key.pattern tells: no pack is issued for such a spec's runs, said here. A
    # warning: the spec is valid, and is stored.
    blind = spec_yaml.replace("^K[0-9]{8}$", "^(K[0-9]{8})?$")
    validated = client.post("/v1/specs/validate", content=blind).json()
    assert (validated["valid"], [(i["pointer"], i["code"]) for i in validated["issues"]]) == (
        True,
        [("/key/pattern", "key_pattern_empty"), ("/key/source/keys", "inline_keys_stored")],
    )
    assert client.post("/v1/specs", content=blind).status_code == 201


def _form(spec_yaml: str, **targets: str | None) -> dict[str, Any]:
    """``spec_yaml`` as the builder sends it: each side's target and request out of the text and in the
    side's form. ``targets`` gives a side's form another target, or with None leaves that form empty."""
    doc = parse_yaml_text(spec_yaml)
    sides = {
        side: {name: entry.pop(name) for name in ("target", "request")}
        for side, entry in doc["sides"].items()
    }
    for side, target in targets.items():
        sides[side]["target"] = target
    return {"text": yaml.safe_dump(doc), "sides": {s: f for s, f in sides.items() if f["target"] is not None}}


def _issues(answer: Any) -> tuple[int, str, list[tuple[str, str]]]:
    return (*said(answer), [(issue["pointer"], issue["code"]) for issue in answer.json()["issues"]])


def test_a_form_validates_and_saves_as_its_document(client: TestClient, spec_yaml: str, audit: Any) -> None:
    """A JSON body holding ``text`` is a form: its text parsed, each sent side's target and request put in
    place of the text's. It validates and is stored as that document is, and its issues point into it."""
    form = _form(spec_yaml)
    assert "target" not in form["text"] and "request" not in form["text"]
    validated = client.post("/v1/specs/validate", json=form).json()
    assert (validated["valid"], [issue["code"] for issue in validated["issues"]]) == (
        True,
        ["inline_keys_stored"],
    )
    created = client.post("/v1/specs", json=form)
    assert created.status_code == 201, created.text
    spec_id = created.json()["spec_id"]
    assert client.post("/v1/specs", content=spec_yaml).json()["spec_id"] == spec_id  # the same document
    stored = client.get(f"/v1/specs/{spec_id}").json()
    assert stored["body"]["sides"]["b"] == {
        "role": "new",
        "target": "svc-b",
        "request": GET,
        "truncation": {"countPath": "$.n", "cap": 9},
    }
    # What a side's form sends replaces what the text holds of that side, and only of that side.
    stale = parse_yaml_text(spec_yaml)
    stale["sides"]["a"] |= {"target": "svc-z", "request": {"method": "POST", "path": "/old"}}
    over = {"text": yaml.safe_dump(stale), "sides": {"a": form["sides"]["a"]}}
    assert client.post("/v1/specs", json=over).json()["spec_id"] == spec_id
    # A side sent without a request has none; an unknown target is found where the joined document has it.
    bare = client.post("/v1/specs", json=over | {"sides": {"a": {"target": "svc-a"}}})
    assert _issues(bare) == (422, "invalid spec", [("/sides/a", "schema.required")])
    unknown = client.post("/v1/specs/validate", json=_form(spec_yaml, b="svc-z")).json()
    assert (
        unknown["valid"],
        [issue["pointer"] for issue in unknown["issues"] if issue["severity"] == "error"],
    ) == (
        False,
        ["/sides/b/target"],
    )
    # One mapping for both sides in the text (an alias): each side still gets its own form.
    doc = parse_yaml_text(form["text"])
    aliased = form["text"].replace(
        yaml.safe_dump({"sides": doc["sides"]}),
        "sides: {a: &s {role: old, truncation: {countPath: $.n, cap: 9}}, b: *s}\n",
    )
    assert aliased != form["text"]
    both = client.post("/v1/specs", json=form | {"text": aliased})
    assert both.status_code == 201, both.text
    targets = client.get(f"/v1/specs/{both.json()['spec_id']}").json()["body"]["sides"]
    assert (targets["a"]["target"], targets["b"]["target"]) == ("svc-a", "svc-b")
    # A form that is not one is the request's error, and never quoted.
    for wrong, at in (
        ({"sides": {"c": {"target": "svc-a"}}}, "/sides/c/[key]"),
        ({"sides": {"a": {"request": GET}}}, "/sides/a/target"),
        ({"sides": {"a": {"target": "svc-a", "secret": "s3cret"}}}, "/sides/a/secret"),
        ({"keys": [RAW_KEY]}, "/keys"),
    ):
        refused = client.post("/v1/specs", json=form | wrong)
        assert (said(refused), [issue["pointer"] for issue in refused.json()["issues"]]) == (
            (422, "invalid request"),
            [at],
        )
        assert "s3cret" not in refused.text
    broken = client.post("/v1/specs", json=form | {"text": "key: [1,"})
    assert _issues(broken)[2] == [("", "yaml_syntax")]
    assert audit(client) == ["spec.create"] * 4


def test_a_stored_form_saved_unchanged_is_the_same_version(client: TestClient, spec_yaml: str) -> None:
    """``?form=1`` answers a stored spec as the builder's form; sent back as it came, it is the stored
    version, not a new one. A business code's inline keys are shown in clear, so they come back as keys."""
    coded = spec_yaml.replace("class: identifier", "class: business_code")
    spec_id = client.post("/v1/specs", content=coded).json()["spec_id"]
    assert "form" not in client.get(f"/v1/specs/{spec_id}").json()
    form = client.get(f"/v1/specs/{spec_id}", params={"form": 1}).json()["form"]
    assert form["sides"] == {
        "a": {"target": "svc-a", "request": GET},
        "b": {"target": "svc-b", "request": GET},
    }
    rest = parse_yaml_text(form["text"])
    assert rest["sides"] == {
        side: {"role": role, "truncation": {"countPath": "$.n", "cap": 9}}
        for side, role in (("a", "old"), ("b", "new"))
    }
    assert rest["key"]["source"]["keys"] == [RAW_KEY] and not form["text"].startswith(("#", "\n"))
    again = client.post("/v1/specs", json=form)
    assert (again.status_code, again.json()["spec_id"]) == (201, spec_id)
    assert [item["spec_id"] for item in client.get("/v1/specs").json()["items"]] == [spec_id]


def test_a_normalised_inline_key_is_a_new_version(client: TestClient, spec_yaml: str) -> None:
    """A stored spec's inline keys are shown as its runs take them, normalised: a key the rule changes
    comes back changed, and the form saved unchanged is then another version of the name."""
    raw = spec_yaml.replace("class: identifier", "class: business_code, normalize: [trim, upper]").replace(
        "keys: [K12345678]", 'keys: [" k12345678 "]'
    )
    spec_id = client.post("/v1/specs", content=raw).json()["spec_id"]
    form = client.get(f"/v1/specs/{spec_id}", params={"form": 1}).json()["form"]
    assert parse_yaml_text(form["text"])["key"]["source"]["keys"] == [RAW_KEY]
    again = client.post("/v1/specs", json=form)
    assert again.status_code == 201 and again.json()["spec_id"] != spec_id
    assert [(item["name"], item["version"]) for item in client.get("/v1/specs").json()["items"]] == [
        ("api-demo", "1.0.0")
    ] * 2
    assert client.post("/v1/specs", json=form).json()["spec_id"] == again.json()["spec_id"]  # now unchanged


TABLE_SPEC = """\
apiVersion: deltasleuth/v1
kind: ComparisonSpec
metadata: {name: tables-demo, version: "1.0.0", owner: tests}
normalizerVersion: "1.1.0"
key: {name: item_id, type: string, pattern: "^K[0-9]{8}$", source: {kind: list, keys: [K12345678]}}
sides:
  a:
    role: old
    target: svc-a
    request: {method: GET, path: "/items/{{ key }}"}
    truncation: {countPath: $.n, cap: 9}
  b:
    role: new
    target: db
    table: {name: items, keyColumns: [id], columns: [price]}
compare: {}
recheck: {delay: 1s, times: 1, oneShot: {delay: 0s, times: 1}}
"""
DATABASE = "  db:\n    dsn_ref: file:dsn\n"  # a database target: any DSN, never opened here


def test_a_table_spec_form_round_trips(open_client: OpenClient) -> None:
    """A side holding ``table`` is no form side: its target and table stay in the text, the split names no
    form for it, and sending one is refused."""
    with open_client(targets=TARGETS + DATABASE) as client:
        made = client.post("/v1/specs", content=TABLE_SPEC)
        assert made.status_code == 201, made.text
        spec_id = made.json()["spec_id"]
        form = client.get(f"/v1/specs/{spec_id}", params={"form": 1}).json()["form"]
        assert form["sides"] == {"a": {"target": "svc-a", "request": GET}}
        kept = parse_yaml_text(form["text"])["sides"]
        assert kept["a"] == {"role": "old", "truncation": {"countPath": "$.n", "cap": 9}}
        assert kept["b"] == parse_yaml_text(TABLE_SPEC)["sides"]["b"]
        assert client.post("/v1/specs", json=form).json()["spec_id"] == spec_id
        validated = client.post("/v1/specs/validate", json=form).json()
        assert validated["valid"] and validated["form"]["sides"] == form["sides"]
        sent = form | {"sides": form["sides"] | {"b": {"target": "svc-b", "request": GET}}}
        table_side = (422, "side b is a table side: it has no request")
        assert [said(client.post(path, json=sent)) for path in ("/v1/specs", "/v1/specs/validate")] == [
            table_side
        ] * 2
        for body in (form, sent):
            tried = client.post("/v1/specs/try", json={"form": body, "side": "b", "key": RAW_KEY})
            assert said(tried) == table_side


def test_stand_in_inline_keys_are_refused(client: TestClient, spec_yaml: str, audit: Any) -> None:
    """A stored spec's inline keys of a class other than business_code are shown as tokens. Such a form is
    refused wherever it is sent: a token saved as a key would be asked of the endpoints."""
    spec_id = client.post("/v1/specs", content=spec_yaml).json()["spec_id"]
    form = client.get(f"/v1/specs/{spec_id}", params={"form": 1}).json()["form"]
    (shown,) = parse_yaml_text(form["text"])["key"]["source"]["keys"]
    assert tokenise.is_token(shown) and RAW_KEY not in json.dumps(form)
    stand_ins = (
        422,
        "the inline keys of this text are stand-ins: type the keys again, or declare another key source and"
        " give keys with the run",
    )
    refused = [
        client.post("/v1/specs", json=form),
        client.post("/v1/specs/validate", json=form),
        client.post("/v1/specs/try", json={"form": form, "side": "a", "key": RAW_KEY}),
    ]
    assert [said(answer) for answer in refused] == [stand_ins] * 3
    typed = form | {"text": form["text"].replace(shown, RAW_KEY)}
    assert client.post("/v1/specs", json=typed).json()["spec_id"] == spec_id  # typed again: the same version
    assert audit(client) == ["spec.create"] * 2 and audited(client, "target.try") == []


def test_validate_answers_the_split_of_pasted_yaml(client: TestClient, spec_yaml: str) -> None:
    """A whole spec pasted into the text, the forms empty: the answer's ``form`` holds each service side's
    target and request, for the builder to fill its forms with, and the text without them."""
    pasted = client.post("/v1/specs/validate", json={"text": spec_yaml, "sides": {}}).json()
    assert pasted["valid"] and pasted["form"]["sides"] == {
        "a": {"target": "svc-a", "request": GET},
        "b": {"target": "svc-b", "request": GET},
    }
    rest = parse_yaml_text(pasted["form"]["text"])
    assert set(rest["sides"]["a"]) == set(rest["sides"]["b"]) == {"role", "truncation"}
    assert client.post("/v1/specs/validate", json=pasted["form"]).json() == pasted  # split and joined again
    # A side the text does not hold has an empty form; one without a target keeps what it has.
    doc = parse_yaml_text(spec_yaml)
    del doc["sides"]["b"], doc["sides"]["a"]["target"]
    partial = client.post("/v1/specs/validate", json={"text": yaml.safe_dump(doc)}).json()
    assert not partial["valid"] and partial["form"]["sides"] == {"a": {"request": GET}, "b": {}}
    # Neither a text that does not parse, nor one that is no mapping, nor a document sent whole, has one.
    for body in ({"text": "key: [1,"}, {"text": "- a list"}, {"text": ""}):
        assert "form" not in client.post("/v1/specs/validate", json=body).json()
    assert "form" not in client.post("/v1/specs/validate", content=spec_yaml).json()
    assert "form" not in client.post("/v1/specs/validate", json=parse_yaml_text(spec_yaml)).json()


def test_form_text_falls_back_to_json(client: TestClient, spec_yaml: str) -> None:
    """The text is YAML rendered from the document. Where that YAML would not read back as the document
    (a string YAML 1.2 reads as a number), the text is the document's JSON, indented, which does."""
    doc = parse_yaml_text(spec_yaml.replace("class: identifier", "class: business_code"))
    doc["metadata"]["owner"] = "0o17"  # YAML 1.1, the renderer's, writes it plain; 1.2 reads the integer 15
    spec_id = client.post("/v1/specs", json=doc).json()["spec_id"]
    form = client.get(f"/v1/specs/{spec_id}", params={"form": 1}).json()["form"]
    assert (
        form["text"].startswith('{\n  "key": {') and json.loads(form["text"])["metadata"]["owner"] == "0o17"
    )
    assert json.loads(form["text"])["sides"]["a"] == {
        "role": "old",
        "truncation": {"countPath": "$.n", "cap": 9},
    }
    plain = routes.split(parse_yaml_text(spec_yaml))["text"]
    assert plain.startswith("apiVersion: deltasleuth/v1\n") and "{\n" not in plain
    # The JSON text is a form's text like the YAML: saved, it is the stored version.
    assert client.post("/v1/specs", json=form).json()["spec_id"] == spec_id
    # U+0085, which the reader takes for a line break in either text: the JSON holds it as an escape, so
    # the form saved unchanged is the stored version, the character still there (not a space).
    doc["metadata"] |= {"version": "1.1.0", "owner": "payments\x85team"}
    nel = client.post("/v1/specs", json=doc).json()["spec_id"]
    form = client.get(f"/v1/specs/{nel}", params={"form": 1}).json()["form"]
    assert '"owner": "payments\\u0085team"' in form["text"] and "\x85" not in form["text"]
    again = client.post("/v1/specs", json=form)
    assert (again.status_code, again.json()["spec_id"]) == (201, nel) and nel != spec_id
    assert client.get(f"/v1/specs/{nel}").json()["body"]["metadata"]["owner"] == "payments\x85team"
    for raw in "\x7f\x80\x85\x9f\u2028\u2029\ufffe\uffff":  # in a name too: what the reader takes no raw
        named = {"metadata": {f"a{raw}b": f"c{raw}d", "n": "0o17"}}
        assert parse_yaml_text(routes.split(named)["text"]) == named
    # A document neither text reads back as has no form: nothing is offered to save as it.
    assert routes.split({"n": float("inf")}) is None


def test_form_and_masking_run_off_the_loop(
    client: TestClient, spec_yaml: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec is up to 4 MiB and an answer up to 1 MiB: splitting the one, and parsing, masking and
    writing the other (megabytes of text), run in a thread, so neither stalls the API or the worker's
    keys."""
    loop, work = client.portal.call(threading.get_ident), []

    def watched(name: str) -> None:
        real = getattr(routes, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            work.append((name, threading.get_ident() == loop))
            return real(*args, **kwargs)

        monkeypatch.setattr(routes, name, call)

    for name in ("split", "parse_json", "mask_raw", "ok"):
        watched(name)
    coded = spec_yaml.replace("class: identifier", "class: business_code")
    spec_id = client.post("/v1/specs", content=coded).json()["spec_id"]
    form = client.get(f"/v1/specs/{spec_id}", params={"form": 1}).json()["form"]
    assert client.post("/v1/specs/validate", json=form).json()["valid"]
    tried = client.post("/v1/specs/try", json={"form": form, "side": "b", "key": RAW_KEY})
    assert tried.status_code == 200 and tried.json()["answer"]
    assert work == [
        ("ok", True),  # the spec stored
        ("split", False),
        ("ok", True),
        ("split", False),
        ("ok", True),
        ("parse_json", False),
        ("mask_raw", False),
        ("ok", False),  # the tried side's answer
    ]


ANSWER = (
    b'{"id": "%s", "price": ' + PRICE.encode() + b', "n": 1, "open": true, "closed": null,'
    b' "contact": {"name": "Ada Lovelace", "ACC00012345": {"n": 1}, "a b": "Ada Lovelace"},'
    b' "phones": ["+44 20 7946 0000", 2.5]}'
)
Asked = list[tuple[str, str, bytes]]  # each request an endpoint received: its method, path and body


@pytest.fixture
def service() -> Iterator[tuple[str, Asked, list[Callable[[], None]]]]:
    """A local endpoint, the requests it received and what to call as each arrives. By the path's first
    segment: ``items`` answers ANSWER about the key, ``query`` the JSON body it was sent, ``big`` JSON over
    1 MiB, ``paged`` two pages of 0.6 MiB, ``text`` plain text, ``deep`` JSON nested 300 deep, ``down`` a
    503, ``denied`` a 401 with a body, ``echo`` the headers it was sent (by name, as a list, and as member
    names) and ``wide/<n>`` a list of n numbers written ``1e3999``."""
    asked: Asked = []
    hooks: list[Callable[[], None]] = []

    class Endpoint(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            sent = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            asked.append((self.command, self.path, sent))
            for hook in hooks:
                hook()
            kind, _, rest = self.path.removeprefix("/").partition("/")
            status, media = {"down": 503, "denied": 401}.get(kind, 200), "application/json"
            body = b'{"said": "s3cret-echo"}'
            if kind == "items":
                body = ANSWER % rest.encode()
            elif kind == "query":
                body = b'{"asked": ' + sent + b"}"
            elif kind == "big":
                body = b'{"pad": "' + b"x" * 2**20 + b'"}'
            elif kind == "paged":
                last = "cursor=2" in rest
                body = json.dumps({"items": ["x" * 600_000], "next": None if last else "2"}).encode()
            elif kind == "text":
                body, media = b"hello", "text/plain"
            elif kind == "deep":
                body = b"[" * 300 + b"]" * 300
            elif kind == "echo":
                sent = dict(self.headers.items())
                names = {value: name for name, value in sent.items()}
                body = json.dumps({"headers": sent, "values": list(sent.values()), "by": names}).encode()
            elif kind == "wide":
                body = b"[" + b",".join([b"1e3999"] * int(rest.partition("/")[0])) + b"]"
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", asked, hooks
    server.shutdown()
    server.server_close()


def _declare(client: TestClient, url: str, headers: dict[str, str] | None = None) -> None:
    """own-a and own-b, declared in the product at the endpoint."""
    body = {"base_url": url, "allow_private_network": True}
    made = [client.put(f"/v1/targets/own-{side}", json=body, headers=headers) for side in "ab"]
    assert [answer.status_code for answer in made] == [201, 201], [answer.text for answer in made]


def _try(client: TestClient, form: dict[str, Any], side: str = "a", key: str = RAW_KEY, **kw: Any) -> Any:
    return client.post("/v1/specs/try", json={"form": form, "side": side, "key": key}, **kw)


def _pathed(form: dict[str, Any], side: str, **request: Any) -> dict[str, Any]:
    """``form`` with the side's request changed."""
    changed = form["sides"][side] | {"request": form["sides"][side]["request"] | request}
    return form | {"sides": form["sides"] | {side: changed}}


def test_try_answers_masked(client: TestClient, service: tuple[str, Asked, Any], spec_yaml: str) -> None:
    """Outside a trial the answer shows its structure alone: every string and number a stand-in, a member
    name that is data too; equal values get equal stand-ins, on both sides. Neither a value, the key nor
    the endpoint's address is in it."""
    url, asked, _ = service
    _declare(client, url)
    form = _form(spec_yaml, a="own-a", b="own-b")
    tried = _try(client, form)
    assert tried.status_code == 200, tried.text
    shown = tried.json()
    answer, ms = shown.pop("answer"), shown.pop("ms")
    assert ms > 0 and shown == {
        "side": "a",
        "target": "own-a",
        "method": "GET",
        "status": 200,
        "error_class": None,
        "error_detail": None,
        "attempts": 1,
        "pages": 1,
        "bytes": len(ANSWER % RAW_KEY.encode()),
        "content_type": "application/json",
        "note": None,
        "raw": False,
    }
    tok = client.app.state.ctx.tokeniser
    text, number = (lambda v: tok.token("free_text", v)), (lambda v: tok.token("free_number", Decimal(v)))
    assert answer == {
        "id": text(RAW_KEY),
        "price": number(PRICE),
        "n": number("1"),
        "open": True,
        "closed": None,
        "contact": {
            "name": text("Ada Lovelace"),
            text("ACC00012345"): {"n": number("1")},
            text("a b"): text("Ada Lovelace"),
        },
        "phones": [text("+44 20 7946 0000"), number("2.5")],
    }
    assert re.fullmatch(STAND_IN, answer["id"]) and re.fullmatch(STAND_IN, answer["price"])
    for clear in (RAW_KEY, "Ada", PRICE, "ACC00012345", "7946", "2.5", url, "127.0.0.1", "/items"):
        assert clear not in tried.text, clear
    assert _try(client, form, "b").json()["answer"] == answer  # the other side's equal values
    assert [(method, path) for method, path, _ in asked] == [("GET", f"/items/{RAW_KEY}")] * 2


def test_try_answers_raw_in_a_trial_only(
    open_client: OpenClient, callers: Any, service: tuple[str, Asked, Any], spec_yaml: str
) -> None:
    """A trial (tokens mode on a plain-http loopback address, as deploy/up.sh writes) reads the answer as
    it came, exact numbers included, and its audit row says so; tokens mode behind https reads stand-ins."""
    url, asked, _ = service
    form = _form(spec_yaml, a="own-a", b="own-b")
    with open_client(auth=TRIAL) as client:
        author = callers(client).token("spec_author")
        _declare(client, url, author)
        tried = _try(client, form, headers=author)
        assert tried.status_code == 200, tried.text
        assert (tried.json()["raw"], tried.json()["note"]) == (True, None)
        came = json.loads(ANSWER % RAW_KEY.encode())
        assert json.loads(tried.text)["answer"] == came and f'"price": {PRICE}' in tried.text
        body = {"id": "{{ key }}", "fields": ["price"]}
        posted = _try(
            client, _pathed(form, "b", method="POST", path="/query", body=body), "b", headers=author
        )
        assert posted.status_code == 200, posted.text
        assert (posted.json()["method"], posted.json()["answer"]) == (
            "POST",
            {"asked": body | {"id": RAW_KEY}},
        )
        assert asked[-1][:2] == ("POST", "/query") and json.loads(asked[-1][2])["fields"] == ["price"]
        assert [detail["raw"] for _, _, detail in audited(client, "target.try")] == [True, True]
        assert _try(client, form, headers=callers(client).token("operator")).status_code == 403
    with open_client(auth={}) as client:  # an office in tokens mode: no trial
        author = callers(client).token("spec_author")
        masked = _try(client, _form(spec_yaml), headers=author)
        assert masked.status_code == 200, masked.text
        assert masked.json()["raw"] is False and re.fullmatch(STAND_IN, masked.json()["answer"]["id"])
        assert RAW_KEY not in masked.text and "1.0000" not in masked.text
        assert [detail["raw"] for _, _, detail in audited(client, "target.try")] == [True, True, False]


def test_try_over_1_mib_and_not_json(
    client: TestClient, service: tuple[str, Asked, Any], spec_yaml: str
) -> None:
    """No answer is shown of a body over 1 MiB (its pages together), of one that is not JSON, or of a 401,
    whose body is never read: the status line says which."""
    url, _, _ = service
    _declare(client, url)
    form = _form(spec_yaml, a="own-a", b="own-b")

    def tried(path: str, text: str | None = None) -> tuple[Any, ...]:
        answer = _try(client, _pathed(form if text is None else form | {"text": text}, "a", path=path))
        assert answer.status_code == 200, answer.text
        names = ("status", "error_class", "error_detail", "pages", "bytes", "content_type", "answer", "note")
        return tuple(answer.json()[name] for name in names)

    over = "response body exceeds 1048576 bytes"
    assert tried("/big/{{ key }}") == (200, "resource_limit", over, 1, None, "application/json", None, None)
    assert tried("/text/{{ key }}") == (200, None, None, 1, 5, "text/plain", None, "the answer is not JSON")
    assert tried("/deep/{{ key }}") == (
        200, None, None, 1, 600, "application/json", None, "nesting deeper than 256"
    )  # fmt: skip
    assert tried("/denied/{{ key }}") == (
        401,
        "auth_error",
        "HTTP 401",
        1,
        None,
        "application/json",
        None,
        None,
    )
    paged = parse_yaml_text(form["text"])
    paged["sides"]["a"]["pagination"] = {
        "kind": "cursor", "itemsPath": "$.items", "cursorPath": "$.next", "cursorParam": "cursor"
    }  # fmt: skip
    status, error_class, detail, pages, size, *_, answer, note = tried(
        "/paged/{{ key }}", yaml.safe_dump(paged)
    )
    assert (status, error_class, pages, size, answer, note) == (200, "resource_limit", 2, None, None, None)
    assert re.fullmatch(r"page 2: response body exceeds 4\d{5} bytes", detail)  # what page 1 left of 1 MiB


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_try_reads_its_form_as_a_document(
    client: TestClient, service: tuple[str, Asked, Any], spec_yaml: str, number: str
) -> None:
    """The body of a trial is read by the documents reader, as a spec sent to be saved is. A number JSON
    has none of, or one no float holds, in a request's body or query is the document's issue: nothing is
    audited and nothing is asked of the endpoint. Validating and saving the same form say the same; so
    does a text holding the number. A nesting or a repeated member no document may hold is refused too."""
    url, asked, _ = service
    _declare(client, url)
    form = _form(spec_yaml, a="own-a", b="own-b")
    json_body = {"content-type": "application/json"}
    refused = (422, "invalid spec", [("", "number_out_of_range" if "e" in number else "json_syntax")])
    posted = {"method": "POST", "path": "/query", "body": {"limit": 0}}
    for request in (posted, {"query": {"limit": 0}}):
        formed = json.dumps(_pathed(form, "a", **request)).replace('"limit": 0', f'"limit": {number}')
        assert number in formed
        tried = '{"form": ' + formed + ', "side": "a", "key": "' + RAW_KEY + '"}'
        assert _issues(client.post("/v1/specs/try", content=tried, headers=json_body)) == refused
        assert _issues(client.post("/v1/specs", content=formed, headers=json_body)) == refused
        validated = client.post("/v1/specs/validate", content=formed, headers=json_body)
        assert (validated.status_code, validated.json()["valid"], "form" in validated.json()) == (
            200,
            False,
            False,
        )
        assert [(i["pointer"], i["code"]) for i in validated.json()["issues"]] == refused[2]
    if "e" in number:  # a YAML number: in a form's text, and in a document sent whole
        compare = f"compare: {{fields: [{{path: $.n, compare: {{tolerance: {{abs: {number}}}}}}}]}}"
        whole, text = (text.replace("compare: {}", compare) for text in (spec_yaml, form["text"]))
        assert number in whole and number in text
        assert _issues(_try(client, form | {"text": text})) == refused
        assert _issues(client.post("/v1/specs", content=whole)) == refused
        assert _issues(client.post("/v1/specs", json=form | {"text": text})) == refused
        for sent in ({"content": whole}, {"json": form | {"text": text}}):
            validated = client.post("/v1/specs/validate", **sent).json()
            assert [(i["pointer"], i["code"]) for i in validated["issues"]] == refused[2]
    deep: Any = "x"
    for _ in range(200):
        deep = [deep]
    nested = _try(client, _pathed(form, "a", method="POST", path="/query", body={"rows": deep}))
    assert _issues(nested) == (422, "invalid spec", [("", "document_too_deep")])
    twice = json.dumps({"form": form, "side": "b", "key": RAW_KEY}).replace('"side"', '"side": "a", "side"')
    repeated = client.post("/v1/specs/try", content=twice, headers=json_body)
    assert _issues(repeated) == (422, "invalid spec", [("", "json_duplicate_key")])
    assert asked == [] and audited(client, "target.try") == []
    assert client.get("/v1/specs").json()["items"] == []


def test_a_raw_answer_is_not_written_far_beyond_what_was_read(
    open_client: OpenClient, callers: Any, service: tuple[str, Asked, Any], spec_yaml: str
) -> None:
    """A body under 1 MiB may hold thousands of numbers of 4,000 digits each, and the answer as it came
    writes every digit: 100 kB read was 58 MB answered. Past RAW_CHARS of numbers there is no answer and
    the note says why; up to it the answer is as it came."""
    url, _, _ = service
    form = _form(spec_yaml, a="own-a", b="own-b")
    most = routes.RAW_CHARS // 4000
    with open_client(auth=TRIAL) as client:
        author = callers(client).token("spec_author")
        _declare(client, url, author)

        def tried(numbers: int) -> Any:
            answer = _try(client, _pathed(form, "a", path=f"/wide/{numbers}/{{{{ key }}}}"), headers=author)
            assert answer.status_code == 200, answer.text
            return answer

        wide = tried(14_600)  # 102,201 bytes
        shown = wide.json()
        assert (shown["bytes"], shown["answer"], shown["note"], shown["raw"]) == (
            102_201,
            None,
            "the answer's numbers, written in full, take over 4194304 characters",
            True,
        )
        assert len(wide.content) < 1000
        assert tried(most + 1).json()["answer"] is None
        held = tried(most)
        assert held.json()["note"] is None and held.json()["answer"] == [10**3999] * most
        assert len(held.content) < routes.RAW_CHARS + 2 * most + 1000


def test_try_never_shows_a_credential_of_its_target(
    open_client: OpenClient, callers: Any, service: tuple[str, Asked, Any], spec_yaml: str
) -> None:
    """A service may answer the request it was sent (an echo route, an error listing its headers). Each
    string of the answer that is or holds a credential stored for the target, a member's name too, is
    replaced by one marker, in a trial's answer as it came and in the stand-ins' structure alike; every
    other string is as it was."""
    url, asked, _ = service
    form = _pathed(_form(spec_yaml, a="own-a", b="own-b"), "a", path="/echo/{{ key }}")
    form = _pathed(form, "b", path="/echo/{{ key }}")
    consents = {"base_url": url, "allow_private_network": True, "allow_insecure_http": True}
    cookie = {"kind": "header", "header": "X-Api-Secret", "more": [{"header": "X-CSRF-Token"}]}
    secrets = ("s3cr3t-cred-value", "antiForgeryValue", "b3arerCredential")
    declared = {
        "own-a": {"auth": cookie, "secret": secrets[0], "more_secrets": {"X-CSRF-Token": secrets[1]}},
        "own-b": {"auth": {"kind": "bearer"}, "secret": secrets[2]},
    }
    with open_client(auth=TRIAL) as client:
        author = callers(client).token("spec_author")
        for target, auth in declared.items():
            made = client.put(f"/v1/targets/{target}", json=consents | auth, headers=author)
            assert made.status_code == 201, made.text
        for side in "ab":
            tried = _try(client, form, side, headers=author)
            assert tried.status_code == 200 and tried.json()["raw"] is True, tried.text
            assert not any(secret in tried.text for secret in secrets)
            answer = tried.json()["answer"]
            sent = {name.lower(): value for name, value in answer["headers"].items()}
            mine = ("x-api-secret", "x-csrf-token") if side == "a" else ("authorization",)
            assert [sent[name] for name in mine] == [routes.HIDDEN] * len(mine)
            assert sent["host"] == url.removeprefix("http://") and "deltasleuth" in sent["user-agent"]
            assert answer["values"].count(routes.HIDDEN) == len(mine)
            assert routes.HIDDEN in answer["by"] and answer["by"][sent["host"]].lower() == "host"
    assert len(asked) == 2
    with open_client() as client:  # no trial: a name that reads as a field's would stay as it came
        tried = _try(client, form)
        assert tried.status_code == 200 and tried.json()["raw"] is False, tried.text
        assert not any(secret in tried.text for secret in secrets)
        tok = client.app.state.ctx.tokeniser
        assert tok.token("free_text", routes.HIDDEN) in tried.json()["answer"]["by"]


def test_a_short_credential_hides_only_itself() -> None:
    """A credential under eight characters is hidden where it stands alone, never inside another string:
    ``dev`` must not take ``device`` with it. Two names that held a credential stay two members."""
    held = "s3cr3t-cred-value"
    doc = {
        "device": "phone",
        "dev": ["dev", "developer"],
        "sent": f"Bearer {held}",
        held: 1,
        f"x-{held}": 2,
    }
    assert routes._hidden(doc, ["dev", held]) == {
        "device": "phone",
        routes.HIDDEN: ["[the target's credential]", "developer"],
        "sent": routes.HIDDEN,
        f"{routes.HIDDEN} (2)": 1,
        f"{routes.HIDDEN} (3)": 2,
    }


def test_try_with_the_other_side_empty(
    client: TestClient, service: tuple[str, Asked, Any], spec_yaml: str
) -> None:
    """One endpoint is tried before the other is known: where the other side has no request yet, its form
    empty or its target alone chosen, the side tried stands in for it. An issue of the side tried is said
    once, under that side, and the other side's own only when there is no other. Nothing of that is stored,
    and the form still does not validate as a spec."""
    url, asked, _ = service
    _declare(client, url)
    for side in "ab":
        other = "ab".replace(side, "")
        form = _form(spec_yaml, **{side: f"own-{side}", other: None})
        assert list(form["sides"]) == [side]
        chosen = form | {"sides": form["sides"] | {other: {"target": f"own-{other}"}}}  # no path yet
        for sent in (form, chosen):
            tried = _try(client, sent, side)
            assert tried.status_code == 200, tried.text
            assert (tried.json()["side"], tried.json()["target"], tried.json()["status"]) == (
                side,
                f"own-{side}",
                200,
            )
            assert said(_try(client, sent, other)) == (422, f"side {other} needs a target and a path")
            assert not client.post("/v1/specs/validate", json=sent).json()["valid"]
            keyed = _try(client, _pathed(sent, side, query={"api_key": "abc"}), side)
            assert _issues(keyed) == (
                422,
                "invalid spec",
                [(f"/sides/{side}/request/query/api_key", "schema.propertyNames")],
            )
        roleless = parse_yaml_text(form["text"])
        del roleless["sides"][other]["role"]  # the other side's own: said, there being nothing else
        assert _issues(_try(client, chosen | {"text": yaml.safe_dump(roleless)}, side)) == (
            422,
            "invalid spec",
            [(f"/sides/{other}", "schema.required")],
        )
    assert len(asked) == 4 and client.get("/v1/specs").json()["items"] == []
    assert len(audited(client, "target.try")) == 4


def test_try_is_one_attempt_under_the_targets_gate(
    client: TestClient, service: tuple[str, Asked, Any], spec_yaml: str
) -> None:
    """A run retries a 503 as the spec's execution says; a trial of the endpoint asks once. It asks under
    the target's one gate of the process, as a run's read does: a pause the target asked for holds it."""
    url, asked, _ = service
    _declare(client, url)
    form = _pathed(_form(spec_yaml, a="own-a", b="own-b"), "a", path="/down/{{ key }}")
    assert compile_spec(parse_yaml_text(spec_yaml)).execution.retries.max == 2
    tried = _try(client, form).json()
    assert (tried["status"], tried["error_class"], tried["attempts"], tried["answer"]) == (
        503,
        "http_5xx",
        1,
        {"said": tried["answer"]["said"]},
    )
    assert len(asked) == 1
    gate = client.app.state.ctx.gates["own-a"]
    gate.not_before = time.monotonic() + 3600  # as a Retry-After of an hour leaves it
    held = _try(client, form).json()
    assert (held["status"], held["error_class"], held["attempts"], held["pages"]) == (None, "http_429", 0, 0)
    assert "it was not sent" in held["error_detail"] and len(asked) == 1
    assert client.app.state.ctx.gates["own-a"] is gate and "own-b" not in client.app.state.ctx.gates


def test_try_refusals(
    client: TestClient, service: tuple[str, Asked, Any], spec_yaml: str, tmp_path: Path
) -> None:
    """Each refusal is a 422 saying what to change, and none sends a request."""
    url, asked, _ = service
    _declare(client, url)
    form = _form(spec_yaml, a="own-a", b="own-b")
    needs = (422, "side a needs a target and a path")
    assert said(_try(client, form | {"sides": {}})) == needs
    assert said(_try(client, form | {"sides": {"a": {"target": "own-a"}}})) == needs
    assert said(_try(client, _pathed(form, "a", path=""))) == needs
    assert said(_try(client, form, "c")) == said(_try(client, form, key="")) == (422, "invalid request")
    unknown = _try(client, _form(spec_yaml, a="own-z", b=None))  # the side tried stands in for the other
    assert _issues(unknown) == (422, "invalid spec", [("/sides/a/target", "unknown_target")])  # said once
    refused = (
        422,
        "1 key(s) do not match key.pattern after key.normalize, or are over 1024 bytes or hold NUL"
        " (positions 1)",
    )
    assert said(_try(client, form, key="nope")) == refused
    assert audited(client, "target.try") == []  # none of these was a call to audit
    # A key the request cannot carry: a control character in a header.
    loose = form | {"text": form["text"].replace("^K[0-9]{8}$", "^K.+$")}
    assert loose != form
    carried = _try(client, _pathed(loose, "a", headers={"X-Item": "{{ key }}"}), key="K\x07")
    assert said(carried) == (422, "the key cannot be sent to side a")
    # A header the template may not set, since the target sends it as a credential.
    tenant = {"auth": {"kind": "header", "header": "X-Tenant"}, "secret": "acme-7"}
    consents = {"allow_private_network": True, "allow_insecure_http": True}
    made = client.put("/v1/targets/own-a", json={"base_url": url} | consents | tenant)
    assert made.status_code == 200, made.text
    reserved = _try(client, _pathed(form, "a", headers={"X-Tenant": "other"}))
    assert said(reserved) == (422, "request header 'X-Tenant' may not be set by a template")
    # A stored credential that cannot be read, of the product's list and of the targets file alike.
    folder = product.path(client.app.state.ctx.settings).parent / "secrets"
    (stored,) = folder.iterdir()
    stored.unlink()
    (tmp_path / "svc-a.token").unlink()
    for target in ("own-a", "svc-a"):
        unread = _try(client, _form(spec_yaml, a=target, b="own-b"))
        assert said(unread) == (
            422,
            f"the stored credential of target {target} cannot be read: enter it again",
        )
        assert stored.name not in unread.text and "svc-a.token" not in unread.text
    # An address the guard refuses when it is resolved: a name for this machine, without the consent.
    guarded = client.put("/v1/targets/own-b", json={"base_url": url.replace("127.0.0.1", GUARDED)})
    assert guarded.status_code == 200, guarded.text
    assert said(_try(client, form, "b")) == (
        422,
        "target host resolves to a loopback, private, link-local or reserved address; set"
        " allow_private_network on the target to permit it",
    )
    assert asked == [] and len(audited(client, "target.try")) == 5  # asked for, whatever came of it


def test_try_stores_nothing_and_is_audited_before_the_call(
    client: TestClient, service: tuple[str, Asked, Any], spec_yaml: str, pg_dsn: str
) -> None:
    """A trial of an endpoint leaves one audit row, written before the endpoint is asked: who asked which
    target what, by the request's template and the masked key. No spec, run or evidence is stored."""
    url, asked, hooks = service
    _declare(client, url)
    form = _form(spec_yaml, a="own-a", b="own-b")
    rows: list[int] = []

    def count() -> None:
        with psycopg.connect(pg_dsn) as conn:
            cur = conn.execute("SELECT count(*) FROM ds.audit_log WHERE action = 'target.try'")
            rows.append(cur.fetchone()[0])

    hooks.append(count)
    ctx = client.app.state.ctx
    files = sorted(path for path in ctx.settings.evidence_dir.rglob("*") if path.is_file())
    answers = [
        _try(client, form),
        _try(client, form, key="K87654321"),
        _try(client, _pathed(form, "b", path="/down/{{ key }}"), "b"),
    ]
    assert [answer.status_code for answer in answers] == [200, 200, 200]
    assert rows == [1, 2, 3] and len(asked) == 3  # each row committed before its call arrived
    spec = compile_spec(parse_yaml_text(spec_yaml))
    tried = audited(client, "target.try")
    assert [(actor, target) for actor, target, _ in tried] == [("dev:loopback", "own-a")] * 2 + [
        ("dev:loopback", "own-b")
    ]
    template = json.dumps({"request": GET, "target": "own-a"}, sort_keys=True, separators=(",", ":"))
    first = tried[0][2]
    assert first == {
        "spec": "api-demo",
        "version": "1.0.0",
        "content_hash": first["content_hash"],
        "side": "a",
        "method": "GET",
        "request_sha256": hashlib.sha256(template.encode()).hexdigest(),
        "key_masked": tokenise.mask_key(RAW_KEY, spec, ctx.tokeniser),
        "raw": False,
    }
    assert re.fullmatch("[0-9a-f]{64}", first["content_hash"]) and first["key_masked"].startswith(
        "tok:identifier:"
    )
    assert tried[1][2] == first | {"key_masked": tried[1][2]["key_masked"]} != first  # a template: no key
    assert tried[2][2]["request_sha256"] != first["request_sha256"] and tried[2][2]["side"] == "b"
    assert RAW_KEY not in json.dumps(tried) and url not in json.dumps(tried)
    assert client.get("/v1/specs").json()["items"] == [] and client.get("/v1/runs").json()["items"] == []
    assert sorted(path for path in ctx.settings.evidence_dir.rglob("*") if path.is_file()) == files


def _pairs(n: int) -> list[dict[str, Any]]:
    keys = [f"K{i:08d}" for i in range(1, n + 1)]
    return [
        {"key": k, "a": {"id": k, "price": i + 0.25}, "b": {"data": {"code": k, "amount": i + 0.25}}}
        for i, k in enumerate(keys)
    ]


def test_profile_drafts_a_spec_from_sample_pairs(client: TestClient) -> None:
    names = {"spec_name": "drafted", "target_a": "svc-a", "target_b": "svc-b", "key_name": "item_id"}
    draft = client.post("/v1/profile", json=names | {"pairs": _pairs(25)})
    assert draft.status_code == 200
    body = draft.json()
    assert yaml.safe_load(body["yaml"])["metadata"]["name"] == "drafted"
    assert {row["kind"] for row in body["evidence"]} >= {"select", "align"}
    assert isinstance(body["issues"], list) and isinstance(body["notes"], list)
    refused = client.post("/v1/profile", json=names | {"spec_name": "Not Kebab", "pairs": _pairs(2)})
    assert (refused.status_code, refused.json()["detail"]) == (
        422,
        "spec_name must be lower-case kebab-case (at most 63 characters)",
    )
    assert client.post("/v1/profile", json=names | {"pairs": []}).status_code == 422


@pytest.mark.parametrize("pair", PAIRS)
def test_profile_takes_the_pairs_file_the_mock_fixtures_write(
    client: TestClient, tmp_path: Path, pair: str
) -> None:
    """``python -m mocks.fixtures <pair> --pairs FILE`` writes what the Specs screen's draft form takes as
    its file and sends as ``pairs``: the route drafts from the file's text as it stands, and answers the
    draft ``ds profile`` makes of the same pair's layout-1 samples, with each side's unmatched paths."""
    fixtures.main([pair, "--pairs", str(tmp_path / "pairs.json"), "--out", str(tmp_path / "samples")])
    names = {"spec_name": "drafted", "target_a": "svc-a", "target_b": "svc-b", "key_name": "the_key"}
    text = (tmp_path / "pairs.json").read_text(encoding="utf-8")
    sent = json.dumps(names)[:-1] + ', "pairs": ' + text + "}"
    draft = client.post("/v1/profile", content=sent, headers={"content-type": "application/json"})
    assert draft.status_code == 200, draft.text
    source = FileSampleSource(tmp_path / "samples")
    pairs = [(key, *(cli._document(source.observation(key, side)) for side in Side)) for key in source.keys()]
    assert [(p["key"], p["a"] is None, p["b"] is None) for p in json.loads(text)] == [
        (key, a is None, b is None) for key, a, b in pairs
    ]
    filed = bootstrap(pairs, **names)
    body = draft.json()
    assert body["yaml"] == filed.yaml_text and body["notes"] == filed.notes
    assert len(body["evidence"]) == len(filed.evidence) > 0
    assert body["unmatched"] == {side.value: paths for side, paths in filed.unmatched.items()}
    assert set(body["unmatched"]) == {"a", "b"} and all(body["unmatched"].values())
