"""What the ``ds`` commands share: their options, the exit codes and the handling of failures, the store
opened for a command, the output helpers and the wait on an in-process worker."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import traceback
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from deltasleuth.auth.principal import ROLES, Principal, local
from deltasleuth.config import OidcSettings, Settings, load_settings
from deltasleuth.connectors.base import ConnectorError
from deltasleuth.core.spec import SpecError, SpecIssue, load_spec_file
from deltasleuth.core.spec.documents import in_container
from deltasleuth.core.types import CompiledSpec
from deltasleuth.errors import ours
from deltasleuth.evidence.store import EvidenceError
from deltasleuth.llm.model import LLMError

if TYPE_CHECKING:
    from deltasleuth.context import Context


EXIT_DIFF, EXIT_USAGE, EXIT_INTERNAL, EXIT_INTERRUPTED = 1, 2, 3, 130
NO_APPROVERS = "warning: no IdP group maps to rule_approver or above: nothing can be approved"
# Failures a user can act on, one line each and exit 2: a refusal, or an error of a kind the package defines
# for bad settings, secrets, keys and sheets (``errors.ours``), and these. Anything else is a bug, a
# ValueError or LookupError of Python's or a library's among them: its message may quote a key or a value.
_EXPECTED = (SpecError, ConnectorError, EvidenceError, LLMError, OSError, UnicodeDecodeError)
# And these, by module and name: a command imports the store, the runner and the sign-in only when it uses
# them, and nothing can have raised an exception of a module that was never imported.
_EXPECTED_LATER = (
    ("deltasleuth.context", "ContextError"),
    ("deltasleuth.auth.oidc", "SignInError"),
    ("deltasleuth.runner.compare", "EvidenceMissing"),
    ("deltasleuth.approvals", "ApprovalRequired"),
    ("deltasleuth.approvals", "ApprovalConflict"),
    ("psycopg", "OperationalError"),
)


def _raised(exc: BaseException, module: str, name: str) -> bool:
    """Whether ``exc`` is a ``module.name``, without importing ``module``."""
    return isinstance(exc, getattr(sys.modules.get(module), name, ()))


def _expected(exc: BaseException) -> bool:
    """Whether ``exc`` is a failure a user can act on: ``errors.ours``, or of a type ``_EXPECTED`` or
    ``_EXPECTED_LATER`` names."""
    if ours(exc) or isinstance(exc, _EXPECTED):
        return True
    return any(_raised(exc, module, name) for module, name in _EXPECTED_LATER)


def _o(text: str, *flags: str) -> Any:
    return typer.Option(*flags, help=text)


def _a(text: str) -> Any:
    return typer.Argument(help=text)


ConfigOpt = Annotated[Path | None, _o("Settings file (default: $DS_CONFIG).")]
SpecFile = Annotated[Path, _a("Comparison spec (YAML or JSON).")]
TargetsOpt = Annotated[Path | None, _o("Targets file: check the target rules too.")]
RunArg = Annotated[str, _a("The run id.")]
OutOpt = Annotated[Path, _o("Output directory.")]
CycleOpt = Annotated[int | None, _o("Soak cycle (default: the current one).")]
FailOpt = Annotated[bool, _o("Exit 1 when a key is different.", "--fail-on-diff")]
REASON = "Why, in 1 to 200 characters. It is audited and reaches the SIEM: never personal data."


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(EXIT_USAGE)


def _message(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        where = in_container(exc.filename) if isinstance(exc, FileNotFoundError) and exc.filename else ""
        return f"{exc.strerror or type(exc).__name__}: {exc.filename or ''}".rstrip(": ") + where
    if isinstance(exc, UnicodeDecodeError):
        return f"a file is not valid {exc.encoding} text (byte offset {exc.start}); save it as UTF-8"
    if _raised(exc, "psycopg", "OperationalError"):
        return f"the database is unreachable ({type(exc).__name__}); check database_dsn_ref"
    return str(exc)


def _issues(issues: list[SpecIssue]) -> None:
    for issue in issues:
        typer.echo(f"{issue.severity}: {issue}", err=True)


@contextmanager
def _errors(interrupted: str = "interrupted") -> Iterator[None]:
    """Expected failures: one line each, exit 2. A bug: its traceback without messages, then its type and
    where it was raised, exit 3 (never 1). Ctrl-C: 130, saying ``interrupted``."""
    try:
        yield
    except KeyboardInterrupt:
        typer.echo(interrupted, err=True)
        raise typer.Exit(EXIT_INTERRUPTED) from None
    except Exception as exc:
        if not _expected(exc):
            from deltasleuth.runner.cycle import located

            _traceback(exc)
            typer.echo(
                f"internal error: a bug in DeltaSleuth ({located(exc)}); the traceback above tells where",
                err=True,
            )
            raise typer.Exit(EXIT_INTERNAL) from None
        if isinstance(exc, SpecError):
            _issues(list(exc.issues))  # its message would repeat them
        else:
            typer.echo(f"error: {_message(exc)}", err=True)
        raise typer.Exit(EXIT_USAGE) from None


_CAUSED = "The above exception was the direct cause of the following exception:"
_DURING = "During handling of the above exception, another exception occurred:"


def _traceback(exc: BaseException) -> None:
    """``exc``'s traceback as Python prints it, with the exceptions it chains, but each exception by its type
    alone: a message may quote a key or a value."""
    chained: list[tuple[BaseException, str | None]] = []  # the newest first, each with how it follows
    link: BaseException | None = exc
    while link is not None and not any(link is seen for seen, _ in chained):
        older, note = link.__cause__, _CAUSED
        if older is None and not link.__suppress_context__:
            older, note = link.__context__, _DURING
        chained.append((link, note if older is not None else None))
        link = older
    for n, (link, follows) in enumerate(reversed(chained)):
        if follows and n:
            typer.echo(f"\n{follows}\n", err=True)
        typer.echo("Traceback (most recent call last):", err=True)
        typer.echo("".join(traceback.format_tb(link.__traceback__)), nl=False, err=True)
        typer.echo(type(link).__qualname__, err=True)


def _run[T](work: Coroutine[Any, Any, T], interrupted: str = "interrupted") -> T:
    with _errors(interrupted):
        return asyncio.run(work)


def _in_context[T](
    config: Path | None,
    work: Callable[[Context], Awaitable[T]],
    pool: tuple[int, int] | None = None,
    *,
    apply_migrations: bool = False,
) -> T:
    """``work`` with a Context open on the configured store (``pool``: its least and most connections); the
    audit rows it commits are copied for the SIEM. Only the services (``apply_migrations``) apply pending
    migrations: another command refuses while one is pending, so it never changes the schema under a
    running app."""
    from deltasleuth.context import Context

    _siem()

    async def main() -> T:
        async with Context.open(load_settings(config), pool=pool, apply_migrations=apply_migrations) as ctx:
            return await work(ctx)

    return _run(main())


def _read_keys(path: Path) -> list[str]:
    """One key per line; blank lines and ``#`` comments are skipped."""
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _local(config: Path | None) -> Principal:
    """The invocation's principal, before any work: with the auth settings
    ``DS_OPERATOR`` must name the operator."""
    with _errors():
        return local(required=load_settings(config).auth is not None)


class _Stream(logging.Handler):
    """One line per record on the current ``sys.stdout`` or ``sys.stderr`` (a test runner swaps them)."""

    def __init__(self, stream: str) -> None:
        super().__init__()
        self.stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            out = getattr(sys, self.stream)
            out.write(self.format(record) + "\n")
            out.flush()
        except Exception:
            self.handleError(record)


def _siem(stream: str = "stderr") -> None:
    """Copy each audit row this process commits to ``stream`` as a JSON line, for the SIEM: the
    services' stdout, and a command's stderr, so that its stdout stays its output (a token, JSON). The
    first stream chosen stays."""
    audit = logging.getLogger("deltasleuth.audit")
    if not any(isinstance(h, _Stream) for h in audit.handlers):
        handler = _Stream(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    audit.propagate = False


async def _wrote(ctx: Context, who: Principal, run_id: str, name: str, cycle: int | None) -> None:
    """Audit a report the CLI wrote as ``report.download`` (``reports.downloaded``)."""
    from deltasleuth.reports import downloaded

    await downloaded(ctx, who, run_id, name, cycle)


async def _warned(ctx: Context) -> None:
    """Print each of the licence's warnings on stderr: ``serve``, ``worker``,
    ``run`` and ``compare`` only, and never failing them."""
    from deltasleuth import licence

    await licence.shown(ctx, lambda line: typer.echo(f"warning: {line}", err=True))


def _named(ident: str | None, name: str | None) -> str:
    """An actor, user or token id with its name beside it (the id alone when the name adds nothing)."""
    if ident is None:
        return "-"
    return f"{ident} ({name})" if name and name != ident else ident


def _at(value: datetime | None) -> str:
    return "-" if value is None else value.astimezone(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    def plain(v: Any) -> str:
        return v.isoformat() if isinstance(v, datetime) else str(v)

    return json.dumps(value, default=plain, indent=2, sort_keys=True)


async def _save_spec(
    ctx: Context, path: Path, who: Principal, *, gated: bool = True
) -> tuple[int, CompiledSpec]:
    """Store the spec file to run it (``specs.store``: under SSO only an approved upload runs, gated
    unless ``gated`` is false, as a table pre-check is not); its spec_id and compiled form."""
    from deltasleuth import specs

    stored = await specs.store(ctx, load_spec_file(path), who, to_run=True, gated=gated)
    return stored.spec_id, stored.spec


def _approvers(oidc: OidcSettings) -> bool:
    """Whether an IdP group maps to rule_approver or a role above it."""
    return any(oidc.roles.get(role) for role in ROLES[ROLES.index("rule_approver") :])


@contextmanager
def _stopping(*, once: bool = False) -> Iterator[asyncio.Event]:
    """An event a SIGTERM sets while the block runs: the worker then stops as it does inside ``ds serve``,
    its scans' statements cancelled on their servers and its leases handed back. Without a handler the
    signal ends the process where it stands, or is ignored when it is a container's first process.
    ``once`` (``ds mcp``): the first SIGTERM also leaves the signal without a handler again, so that a
    second ends a process whose loop is blocked and never comes to set the event."""
    stop, loop = asyncio.Event(), asyncio.get_running_loop()

    def stopped(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)
        if once:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

    try:
        previous = signal.signal(signal.SIGTERM, stopped)
    except ValueError:  # not the main thread, which alone takes signals
        yield stop
        return
    try:
        yield stop
    finally:
        signal.signal(signal.SIGTERM, previous or signal.SIG_DFL)  # None: one not set from Python


async def _until[T](ctx: Context, wait: bool, check: Callable[[], Awaitable[tuple[bool, T]]]) -> T:
    """What ``check`` found once it says it is done, or at once without ``wait``. With ``wait`` an
    in-process worker (no schedules) works meanwhile, and ``check`` is asked again every
    ``worker.poll_seconds``; a SIGTERM ends the wait as Ctrl-C does, once the worker has stopped."""
    from deltasleuth.runner import run_worker

    with _stopping() as stop:
        running = asyncio.create_task(run_worker(ctx, stop=stop, schedules=False)) if wait else None
        try:
            while True:
                done, found = await check()
                if done or not wait:
                    return found
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), ctx.settings.worker.poll_seconds)
                if stop.is_set():  # a SIGTERM: as Ctrl-C, once the worker below has stopped
                    raise KeyboardInterrupt
        finally:
            stop.set()
            if running:
                await running


def _write(out: Path, files: dict[str, bytes]) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return [out / name for name in files]


def _notify_warned(settings: Settings) -> None:
    """Warn of each channel with a secret a send cannot read now: ``serve``,
    ``worker`` and ``run`` only, never failing them, as its deliveries retry until it is readable."""
    from deltasleuth.connectors.notify import unreadable

    for name, why in unreadable(settings.notify) if settings.notify is not None else []:
        retrying = "its deliveries retry until it is readable"
        typer.echo(f"warning: notify channel {name}: {why}; {retrying}", err=True)
