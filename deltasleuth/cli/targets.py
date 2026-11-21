"""``ds target check``: a database or files target's pre-flight."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import typer

from deltasleuth.cli.common import ConfigOpt, _a, _o, _run
from deltasleuth.config import load_settings
from deltasleuth.core.spec import load_targets
from deltasleuth.errors import Invalid

target_app = typer.Typer(
    no_args_is_help=True,
    help="Database and files targets: the pre-flight check.",
)


def _shown(text: Any, most: int | None = 64) -> str:
    """What a server says of itself, as a terminal may show it: its printable characters, ``most`` at most
    (None: all of them). A server's text may hold control sequences."""
    return "".join(ch for ch in str(text) if ch.isprintable())[:most]


async def _files_check(
    settings: Any, registry: Any, target: Any, config: Path | None, table: str | None, **asked: Any
) -> dict[str, Any]:
    """A files target's check: where its root lies (judged on the files threads under a deadline as a
    context's opening judges it: a dead mount hangs no check), the root itself and the file reader's
    instance, proved, with the limits its start saw hold; with ``table`` (a path of file.path's grammar,
    and ``asked``: its format and what else to print) the lines ``duck.check`` gives of its files."""
    from deltasleuth.connectors.tables import duck, manifest

    who = f"target {target.target_id}"
    if table is not None and asked["form"] is None:
        raise Invalid(f"{who}: --table of a files target is a path and needs --format csv or parquet")
    if refused := await manifest.opened(settings, registry, Path(config or os.environ["DS_CONFIG"])):
        raise Invalid(refused[0])
    duck.configure(settings, registry)
    mount = await duck.mount(target)
    try:
        made = await duck.configured()
    except duck.Unstarted as exc:
        raise Invalid(duck.unstarted(exc, who, settings.files)) from None
    lines = await duck.check(target, table, **asked) if table is not None else []
    found = {"mount": mount, "version": made.version, "built": duck.reader.NEEDED, "lines": lines}
    return found | {"held": ", ".join(duck.restricted(made))}


@target_app.command("check")
def target_check(
    target_id: Annotated[str, _a("A database or files target of the targets file.")],
    table: Annotated[
        str | None, _o("Also describe this table or view: [SCHEMA.]NAME; of a files target, a file.path.")
    ] = None,
    form: Annotated[str | None, _o("A files target's --table: csv or parquet.", "--format")] = None,
    delimiter: Annotated[str | None, _o("A CSV draft's delimiter, in place of the sniffed one.")] = None,
    header: Annotated[bool, _o("Name a CSV draft's fields from the first record.")] = False,
    count: Annotated[bool, _o("Also read the files whole: rows, and the lines that are not rows.")] = False,
    manifest: Annotated[bool, _o("Also hash the files: sha256sum lines and files_sha256.")] = False,
    config: ConfigOpt = None,
) -> None:
    """A database target's pre-flight, without a store and without reading a row: its address resolved and
    vetted, a session opened as a run opens one (TLS, its settings) and, with --table, the relation and what
    the role may read of it. Exit 2 says why not, as a run's creation would. It alone prints the server's
    full version. A files target's: its mount and the file reader, and with --table
    PATH --format csv|parquet the files the path names, a CSV's sniffed draft or a Parquet side's columns;
    --count and --manifest read the files. It alone prints file names."""
    from deltasleuth.connectors.tables import ENGINES, OBSERVED, preflight

    async def work() -> tuple[str, dict[str, Any], Any]:
        settings = load_settings(config)
        registry = load_targets(settings.targets_file)
        target = registry.get(target_id)
        if target.engine is None:
            raise Invalid(f"target {target_id} is not a database target (dsn_ref or engine)")
        if ENGINES[target.engine].fixed:  # its mount and its file reader
            if form not in (None, "csv", "parquet"):
                raise Invalid("--format is csv or parquet")
            asked = {"form": form, "delimiter": delimiter, "header": header, "count": count}
            found = await _files_check(settings, registry, target, config, table, **asked, listed=manifest)
            return target.engine, found, None
        return target.engine, *(await preflight(target, table))

    engine, server, relation = _run(work())
    if ENGINES[engine].fixed:
        built = " and ".join(server["built"])
        version = f"duckdb {server['version']}, {built} built in"
        process = f"read in a process of its own ({server['held']})"
        typer.echo(f"target {target_id}: files, {server['mount']}, {version}, no extension loads, {process}")
        for line in server["lines"]:  # a header's names are a file's text; a draft is copied whole
            typer.echo(_shown(line, None))
        return
    version, charset = _shown(server["version"]), _shown(server["charset"])
    tls = "yes" if server["tls"] else "no"
    typer.echo(f"target {target_id}: {engine} {version}, character set {charset}, TLS {tls}, session ok")
    if engine in OBSERVED and ".".join(version.split(".")[:2]) != OBSERVED[engine]:
        observed = f"Oracle {OBSERVED['oracle']} and MySQL {OBSERVED['mysql']}"
        typer.echo(f"note: this release is observed on {observed} only")
    if (full := ENGINES[engine].full_charset) is not None and charset != full:
        typer.echo("note: a run takes text keys of up to 500 bytes in this character set")
    if relation is None:
        return
    facts, named = relation.facts, _shown(f"{relation.schema}.{relation.name}", 300)
    typer.echo(f"{named} ({facts['kind']}, consistency {facts['consistency']})")
    rows = [
        (_shown(c.name, 150), _shown(c.declared, 150), c.cls or "-",
         "not comparable" if c.cls is None else "not readable" if not c.readable else "key" * c.keyable)
        for c in relation.columns
    ]  # fmt: skip
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, [*widths, 0], strict=True)]
        typer.echo(("  " + "  ".join(cells)).rstrip())
