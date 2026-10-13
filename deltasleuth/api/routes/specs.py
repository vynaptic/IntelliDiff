"""Specs: upload (YAML, JSON by content type, or the builder's form), list, read, validate, try one
side's request against its endpoint, and draft from sample pairs."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import Field, ValidationError

from deltasleuth import approvals
from deltasleuth.api.deps import Ctx, Payload, SpecAuthor, Viewer, issues_of, masked_body, need, ok, trial
from deltasleuth.connectors.base import KeyRejectedError
from deltasleuth.connectors.rest import RestConnector
from deltasleuth.core.jsonio import JSONParseError, ResourceLimitError, parse_json
from deltasleuth.core.spec import SecretError, SpecError, SpecIssue, compile_spec, validate_spec
from deltasleuth.core.spec.assemble import content_hash
from deltasleuth.core.spec.documents import MAX_DOCUMENT_BYTES, parse_json_text, parse_yaml_text
from deltasleuth.core.types import JSON, Side
from deltasleuth.profiler import bootstrap
from deltasleuth.profiler.draft import render_yaml
from deltasleuth.runner.compare import connect
from deltasleuth.runner.keys import normalise_keys
from deltasleuth.security.tokenise import Tokeniser, is_token, mask_key, mask_raw
from deltasleuth.store import repo

router = APIRouter()
TRY_BYTES = 2**20  # what a tried endpoint may answer, its pages together
RAW_CHARS = 4 * TRY_BYTES  # what the numbers of an answer shown as it came may take, written in full
TABLE_SIDE = "side {} is a table side: it has no request"
STAND_INS = (
    "the inline keys of this text are stand-ins: type the keys again, or declare another key source and give"
    " keys with the run"
)
NOT_JSON = "the answer is not JSON"
TOO_LONG = f"the answer's numbers, written in full, take over {RAW_CHARS} characters"
HIDDEN = "[the target's credential]"  # in place of a string of an answer that holds one
_HELD = 8  # a credential shorter than this is hidden only as a whole string
# Raw in a text, the YAML reader refuses these or reads a line break (U+0085, U+2028, U+2029).
_UNREAD = re.compile("[\x7f-\x9f\u2028\u2029\ufffe\uffff]")


class Pair(Payload):
    key: str = Field(min_length=1)
    a: Any = None  # a side's document; null when that side has none for the key
    b: Any = None


class ProfileIn(Payload):
    spec_name: str
    target_a: str
    target_b: str
    key_name: str
    key_pattern: str | None = None
    pairs: list[Pair] = Field(min_length=1)


class SideIn(Payload):
    target: str
    request: dict[str, Any] | None = None  # absent: the side has none


class Form(Payload):
    """A spec as the builder holds it (the browser has no YAML parser): the document's text without its
    service sides' ``target`` and ``request``, which ``sides`` carries for each side whose form names a
    target."""

    text: str = Field(max_length=MAX_DOCUMENT_BYTES)
    sides: dict[Literal["a", "b"], SideIn] = Field(default_factory=dict)


class TryIn(Payload):
    form: Form
    side: Literal["a", "b"]
    key: str = Field(min_length=1)


def joined(form: Form) -> Any:
    """The document a form stands for: its text parsed, then each side sent with its ``target`` and
    ``request`` in place of the text's (no ``request``: none), so issues point into this document. A side
    the text gives a ``table`` has no form, and inline keys shown as a stored spec's are (``masked_body``)
    are stand-ins, not keys: 422 each."""
    doc = parse_yaml_text(form.text)
    if isinstance(doc, dict) and form.sides and isinstance(sides := doc.setdefault("sides", {}), dict):
        for side, sent in form.sides.items():
            entry = sides.get(side, {})
            if not isinstance(entry, dict):
                continue  # the schema says so
            if "table" in entry:
                raise HTTPException(422, TABLE_SIDE.format(side))
            # a new mapping: the text may give both sides one (an alias)
            kept = {name: value for name, value in entry.items() if name not in ("target", "request")}
            sides[side] = kept | {"target": sent.target}
            if sent.request is not None:
                sides[side]["request"] = sent.request
    key = doc.get("key") if isinstance(doc, dict) else None
    source = key.get("source") if isinstance(key, dict) else None
    keys = source.get("keys") if isinstance(source, dict) else None
    if isinstance(keys, list) and any(isinstance(k, str) and is_token(k) for k in keys):
        raise HTTPException(422, STAND_INS)
    return doc


def _reads_as(text: str, doc: Any) -> bool:
    """Whether the documents reader takes ``text`` for ``doc``, types included."""
    try:
        return json.dumps(parse_yaml_text(text), sort_keys=True) == json.dumps(doc, sort_keys=True)
    except SpecError:
        return False


def split(doc: Mapping[str, Any]) -> dict[str, Any] | None:
    """A spec document as the builder's form, ``joined``'s inverse: ``sides`` holds, for a and for b, the
    ``target`` and ``request`` its side has, but for a side holding ``table``, which has no form; ``text``
    is the rest as YAML, or as indented JSON where that YAML would not read back as it: there a character
    the reader does not take raw (``_UNREAD``) is written as its escape, since it reads a raw U+0085 as a
    line break. None where neither text reads back as the document: a form saved unchanged must be the
    document it was made of."""
    rest = json.loads(json.dumps(doc))  # a copy in which no two members are one object
    entries, sides = rest.get("sides"), {}
    for side in ("a", "b"):
        entry = entries.get(side, {}) if isinstance(entries, dict) else {}
        if isinstance(entry, dict) and "table" not in entry:
            sides[side] = {name: entry.pop(name) for name in ("target", "request") if name in entry}
    text = render_yaml(rest, (), {}).lstrip("\n")
    if not _reads_as(text, rest):
        text = json.dumps(rest, indent=2, ensure_ascii=False) + "\n"
        text = _UNREAD.sub(lambda found: f"\\u{ord(found[0]):04x}", text)
        if not _reads_as(text, rest):
            return None
    return {"text": text, "sides": sides}


async def _text(request: Request) -> str:
    """The request's body as text: a spec document's size at most."""
    body = await request.body()
    if len(body) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, f"a spec document is at most {MAX_DOCUMENT_BYTES} bytes")
    try:
        return body.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SpecError([SpecIssue("", "the document is not UTF-8 text", "document_unreadable")]) from None


async def _document(request: Request) -> tuple[Any, bool]:
    """The request's spec document, and whether it came as the builder's form: a JSON body holding
    ``text`` (``joined``)."""
    text = await _text(request)
    is_json = request.headers.get("content-type", "").startswith("application/json")
    doc = parse_json_text(text) if is_json else parse_yaml_text(text)
    if not (is_json and isinstance(doc, dict) and "text" in doc):
        return doc, False
    try:
        return joined(Form.model_validate(doc)), True
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None


@router.post("/specs", status_code=201)
async def create_spec(ctx: Ctx, who: SpecAuthor, request: Request) -> Response:
    """Validate, compile and store a spec (once per content); 422 lists every issue, or says why the
    store would not keep it as uploaded, storing nothing (docs/mvp/APPROVALS.md §6.2). Every upload is
    audited: its first uploader is the spec's ``created_by``. The answer names the scopes that gate its
    runs under SSO (``needs``) and its active four-eyes approval, if any."""
    spec = compile_spec((await _document(request))[0], targets=ctx.targets)
    if problem := approvals.unstorable(spec.raw):
        raise HTTPException(422, problem)
    async with repo.audited(ctx.pool) as (conn, audit):
        spec_id = await repo.save_spec(conn, spec, created_by=who.actor)
        if problem := await approvals.unstored(conn, spec_id, spec):
            raise HTTPException(422, problem)  # rolls the upload back
        active = await repo.active_spec_approval(conn, spec_id)
        await audit(who, "spec.create", str(spec_id), {"content_hash": spec.content_hash})
    fields = {"name": spec.name, "version": spec.version, "content_hash": spec.content_hash}
    gates = {"needs": approvals.needs(spec), "approval_id": active and active["approval_id"]}
    return ok({"spec_id": spec_id, **fields, **gates}, 201)


@router.post("/specs/validate")
async def validate(ctx: Ctx, who: SpecAuthor, request: Request) -> Response:
    """The document's issues. A form whose text parses is also answered as ``form``, the joined document
    split again (``split``), so text pasted whole fills the forms it left empty."""
    form = {}
    try:
        doc, formed = await _document(request)
        issues = validate_spec(doc, targets=ctx.targets)
        if formed and isinstance(doc, dict) and (again := await asyncio.to_thread(split, doc)):
            form = {"form": again}
    except SpecError as exc:  # the document did not parse
        issues = list(exc.issues)
    return ok({"valid": all(i.severity != "error" for i in issues), "issues": issues_of(issues)} | form)


@router.get("/specs")
async def list_specs(ctx: Ctx, who: Viewer) -> Response:
    async with ctx.pool.connection() as conn:
        return ok({"items": await repo.list_specs(conn)})


@router.get("/specs/{spec_id}")
async def get_spec(ctx: Ctx, who: Viewer, spec_id: int, form: bool = False) -> Response:
    """The stored spec, with who stored it by name; its inline keys are shown as runs show them, masked
    by ``key.class``. ``yaml`` is that body as YAML under two comment lines: the stored document, not the
    upload's text, which is not kept. ``form=1`` adds ``form``: that body as the builder's form
    (``split``), where it has one."""
    async with ctx.pool.connection() as conn:
        row = need(await repo.get_spec(conn, spec_id), f"spec {spec_id}")
        spec = await ctx.spec(conn, spec_id)
        by = row["created_by"]  # None before identities were recorded
        names = await repo.actor_names(conn, [by]) if by else {}
    body = masked_body(row["body"], spec, ctx.tokeniser)
    header = (
        f"{row['name']} {row['version']} (spec {spec_id}), content hash {row['content_hash']}",
        "As stored, parsed: the upload's comments and key order are not kept; inline keys are "
        + ("in clear (key.class business_code)." if spec.key.value_class == "business_code" else "masked."),
    )
    shown = row | {"created_by_name": names.get(by), "body": body, "yaml": render_yaml(body, header, {})}
    if form and (again := await asyncio.to_thread(split, body)):
        shown["form"] = again
    return ok(shown)


def _hidden(doc: JSON, credentials: Sequence[str]) -> JSON:
    """``doc`` with HIDDEN in place of every string, a member's name too, that is one of ``credentials``
    or holds one of at least ``_HELD`` characters: a service may answer the request it was sent. A
    shorter credential (a local app's ``dev``) is hidden only where it stands alone, or it would take
    ``device`` with it."""

    def text(value: str) -> str:
        return HIDDEN if any(c == value or (len(c) >= _HELD and c in value) for c in credentials) else value

    def walk(value: JSON) -> JSON:
        if isinstance(value, str):
            return text(value)
        if isinstance(value, dict):
            members: dict[str, JSON] = {}
            for name, member in value.items():
                shown, n = text(name), 1
                while shown in members:  # two names that held a credential stay two members
                    shown, n = f"{HIDDEN} ({n + 1})", n + 1
                members[shown] = walk(member)
            return members
        return [walk(member) for member in value] if isinstance(value, list) else value

    return walk(doc) if credentials else doc


def _number_chars(doc: JSON) -> int:
    """About what the numbers of ``doc`` take written in full, as ``ok`` writes them: 4,000 characters
    for ``1e3999``."""
    if isinstance(doc, Decimal):
        _, digits, exponent = doc.as_tuple()
        return len(digits) + abs(int(exponent))
    if isinstance(doc, dict):
        return sum(map(_number_chars, doc.values()))
    return sum(map(_number_chars, doc)) if isinstance(doc, list) else 0


def _answer(
    body: bytes | None, tokeniser: Tokeniser | None, credentials: Sequence[str] = ()
) -> tuple[Any, str | None]:
    """A tried endpoint's body, parsed with exact numbers, without the target's ``credentials``
    (``_hidden``) and masked (``tokenise.mask_raw``) unless ``tokeniser`` is None; without a body, or with
    one that is not JSON, None and why. Unmasked it is also None, with why, when its numbers written in
    full would take over RAW_CHARS: a body of TRY_BYTES may hold numbers of 4,000 digits each."""
    if body is None:
        return None, None
    try:
        doc, _ = parse_json(body)
    except JSONParseError:
        return None, NOT_JSON
    except ResourceLimitError as exc:  # its depth, or a number's magnitude: no value
        return None, str(exc)
    doc = _hidden(doc, credentials)
    if tokeniser is not None:
        return mask_raw(doc, tokeniser), None
    return (None, TOO_LONG) if _number_chars(doc) > RAW_CHARS else (doc, None)


def _tried(
    shown: dict[str, Any], body: bytes | None, tokeniser: Tokeniser | None, credentials: Sequence[str]
) -> Response:
    """``try_side``'s response, for a thread: ``shown`` with the answer (``_answer``), written as text
    there too, megabytes of it for a body of TRY_BYTES."""
    answer, note = _answer(body, tokeniser, credentials)
    return ok(shown | {"answer": answer, "note": note, "raw": tokeniser is None})


@router.post("/specs/try")
async def try_side(ctx: Ctx, who: SpecAuthor, sent: Request) -> Response:
    """Send one side's request of a form for one key (``TryIn``), and answer what came back: a run's read
    of that side (the target's gate and breaker, the guard, the spec's timeouts, its pages merged) in one
    attempt, of TRY_BYTES at most, storing nothing. The body is read as a spec document is, so a number or
    a nesting no document may hold is refused before anything is asked. Where the other side has no request
    yet this side stands in for it, so one endpoint is tried before the other is known; the issues of what
    stands in are this side's, and said once. Audited on the target before the call. A trial installation
    (``deps.trial``) reads the parsed answer as it came (``raw``); any other its structure alone, every
    value a stand-in. Neither shows a string holding a credential of the target. No URL is echoed."""
    try:
        body = TryIn.model_validate(parse_json_text(await _text(sent)))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None
    side, doc = body.side, joined(body.form)
    sides = doc.get("sides") if isinstance(doc, dict) else None
    here = sides.get(side) if isinstance(sides, dict) else None
    here = here if isinstance(here, dict) else {}
    target, request = here.get("target"), here.get("request")
    if "table" in here:
        raise HTTPException(422, TABLE_SIDE.format(side))
    if not (target and isinstance(target, str) and isinstance(request, dict) and request.get("path")):
        raise HTTPException(422, f"side {side} needs a target and a path")
    other = "b" if side == "a" else "a"
    there = sides.get(other)
    filled = isinstance(there, dict) and "table" not in there and not isinstance(there.get("request"), dict)
    if filled:  # whatever target the other side names: it has nothing to send yet
        there |= {"target": target, "request": request}
    try:
        spec = compile_spec(doc, targets=ctx.targets)
    except SpecError as exc:
        under = f"/sides/{other}/"
        own = [i for i in exc.issues if not (filled and (i.pointer + "/").startswith(under))]
        raise SpecError(own or exc.issues) from None  # the other side's own, when there is no other
    try:
        (key,) = normalise_keys([body.key], spec)
    except ValueError as exc:  # positions, never a key
        raise HTTPException(422, str(exc)) from None
    cfg, raw = spec.sides[Side(side)], trial(ctx.settings)
    detail = {
        "spec": spec.name,
        "version": spec.version,
        "content_hash": spec.content_hash,
        "side": side,
        "method": request["method"],
        "request_sha256": content_hash({"target": target, "request": request}),  # a template: no key
        "key_masked": mask_key(key, spec, ctx.tokeniser),
        "raw": raw,
    }
    async with repo.audited(ctx.pool) as (_, audit):  # committed before the read, as a compare's row is
        await audit(who, "target.try", target, detail)
    once = replace(spec.execution, retries=replace(spec.execution.retries, max=0))
    try:
        connector = connect(ctx, target, once, max_body_bytes=TRY_BYTES)
    except SecretError:  # never its own text: a targets-file credential's names the file
        unread = f"the stored credential of target {target} cannot be read: enter it again"
        raise HTTPException(422, unread) from None
    credentials = connector.credentials if isinstance(connector, RestConnector) else ()
    try:
        obs = await connector.fetch(key, cfg, run_id="try")
    except KeyRejectedError:
        raise HTTPException(422, f"the key cannot be sent to side {side}") from None
    finally:
        await connector.aclose()
    shown = {
        "side": side,
        "target": target,
        "method": request["method"],
        "status": obs.http_status,
        "error_class": obs.error_class,
        "error_detail": obs.error_detail,
        "attempts": obs.attempts,
        "pages": obs.pages,
        "ms": obs.latency_ms,
        "bytes": None if obs.body is None else len(obs.body),
        "content_type": obs.headers.get("content-type"),
    }
    return await asyncio.to_thread(_tried, shown, obs.body, None if raw else ctx.tokeniser, credentials)


@router.post("/profile")
async def profile(ctx: Ctx, who: SpecAuthor, request: Request) -> Response:
    """Draft a spec from ``{key, a, b}`` sample documents, with its evidence and issues.

    The body is read with exact numbers: the profiler infers decimal places from them.
    """
    try:
        doc, _ = parse_json(await request.body())
    except ValueError:
        raise HTTPException(422, "the request body is not valid JSON") from None
    try:
        req = ProfileIn.model_validate(doc)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from None
    pairs = [(p.key, p.a, p.b) for p in req.pairs]
    draft = await asyncio.to_thread(bootstrap, pairs, **req.model_dump(exclude={"pairs"}))
    issues = validate_spec(parse_yaml_text(draft.yaml_text), targets=ctx.targets)
    return ok({
        "yaml": draft.yaml_text,
        "evidence": [row.as_dict() for row in draft.evidence],
        "notes": draft.notes,
        "unmatched": draft.unmatched,
        "issues": issues_of(issues),
    })  # fmt: skip
