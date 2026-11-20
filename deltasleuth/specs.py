"""Storing a spec version, as the upload route and the CLI's spec files do.

A document is compiled against the targets and stored once per content hash, and every upload is audited as
``spec.create``: its first uploader is the version's ``created_by``. A body the store would not keep as
written is refused, storing nothing. Under SSO a spec the CLI is given to run needs an approved upload: its
stored version is found by its content hash and gated before anything is written, and it is not saved
again; a version that needs an approval and was never uploaded is refused, and one that needs none is
stored. An upload is stored in every mode: the approval is asked for after it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deltasleuth import approvals
from deltasleuth.auth.principal import Principal
from deltasleuth.context import Context
from deltasleuth.core.spec import compile_spec
from deltasleuth.core.types import CompiledSpec
from deltasleuth.errors import Invalid
from deltasleuth.store import repo


@dataclass(frozen=True)
class Stored:
    """A stored spec version: its id, its compiled form and its active four-eyes approval, if any."""

    spec_id: int
    spec: CompiledSpec
    approval_id: str | None


async def store(
    ctx: Context, doc: Any, who: Principal, *, to_run: bool = False, gated: bool = True
) -> Stored:
    """Compile and store the spec document ``doc`` for ``who``. ``to_run``: the spec is stored to be run at
    once (the CLI's), which under SSO needs an approved upload, gated unless ``gated`` is false (a table
    pre-check is not gated). SpecError lists the document's issues; Invalid says why the store would not
    keep it as written; ApprovalRequired is the SSO refusal."""
    spec = compile_spec(doc, targets=ctx.targets)
    if problem := approvals.unstorable(spec.raw):
        raise Invalid(problem)
    if to_run and ctx.settings.auth_mode == "oidc":
        async with ctx.pool.connection() as conn:
            found = await repo.spec_by_hash(conn, spec.content_hash)
            if found is not None:
                if gated:
                    await approvals.gate(ctx, conn, found["spec_id"], spec)
                active = await repo.active_spec_approval(conn, found["spec_id"])
                return Stored(found["spec_id"], spec, active and active["approval_id"])
        if approvals.needs(spec):
            raise approvals.ApprovalRequired(
                f"spec {spec.name} {spec.version} has no approved upload: upload it on the Specs screen,"
                " signed in, and have it approved (a CLI or token upload needs two approvers)"
            )
    async with repo.audited(ctx.pool) as (conn, audit):
        spec_id = await repo.save_spec(conn, spec, created_by=who.actor)
        if problem := await approvals.unstored(conn, spec_id, spec):
            raise Invalid(problem)  # rolls the upload back
        active = await repo.active_spec_approval(conn, spec_id)
        await audit(who, "spec.create", str(spec_id), {"content_hash": spec.content_hash})
    return Stored(spec_id, spec, active and active["approval_id"])
