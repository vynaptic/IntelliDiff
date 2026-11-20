"""``ds``: the DeltaSleuth command line.

    ds init | migrate | serve | worker          set up, then run the API, UI and worker
    ds spec validate SPEC [--targets T]
    ds target check ID [--table [SCHEMA.]NAME]  a database target's pre-flight: firewall, TLS, session, grants
    ds compare KEY --spec SPEC                  one key now (a one-shot run)
    ds run SPEC [--keys F] [--samples DIR] [--soak] [--no-wait]
    ds runs | report RUN                        list runs; write their reports
    ds profile | measure | adjudicate | score   the onboarding tools
    ds llm-probe [--profile ID]
    ds token create|revoke|list                 API tokens
    ds session revoke | users [disable|enable]  break-glass: sessions and people
    ds audit [filters] | verify | auth check    the audit log; its hash chain and anchor; the IdP settings
    ds db grants [--role NAME]                  the app's own database role: its grants, as SQL to run
    ds purge [--dry-run] | hold | release       retention: delete expired evidence, or keep a run's
    ds keys status | fingerprint | migrate      the keyring; seal what releases before 006 stored in clear
    ds approvals [--needed] | revoke            four-eyes approvals; break-glass: revoke one
    ds packs | key | revocations | download     evidence packs, their key register entry and revocations
    ds packs verify FILE.zip --fingerprint HEX  check a pack offline (no settings needed)
    ds licence [--json] | id | show | install | withdraw   a vendor build's licence; the installation id
    ds samples [--spec NAME] [--json]          sign-off samples: every draw, or a name's ledger
    ds samples size --tier T [--failures N]    a sign-off stratum's single-stage size (no settings needed)
    ds labellings [--run R-…] [--json]         labelled accuracy: each labelling in one line
    ds notify [--json] | check | explain | deliveries   alerts and notifications: their state, the channels
    ds notify test | retry | mute | unmute     checked without sending, a cycle's digest; the audited acts
    ds readiness [--programme P] [--json] | pair NAME   cut-over readiness: each pair's gates and state
    ds readiness report | check | verify       its reports; the settings as judged; the stored measures
    ds schedules [--spec NAME] [--json] | show NAME    cron schedules: each one's next due time and firings
    ds schedules next CRON --tz ZONE           a cron's next times in a zone (no settings needed)
    ds schedules add | set | pause | resume | delete   the audited writes; the worker fires
    ds precheck [--json] | show PC-… [--pair N]   table pre-checks: counts and sums of pairs of tables
    ds precheck run [SPEC…] [--spec-id N] [--spec NAME] [--all-specs] [--level L] [--fail-on V]
    ds precheck run --a T[:SCHEMA] --b T[:SCHEMA] [--include G] [--exclude G]   two schemas, by name
    ds precheck cancel PC-… | report PC-… --out DIR | draft PC-… (--pair N | --all) --out DIR
    ds mcp [--role viewer|operator] [--host-channel]   the MCP tools for your own agent, over stdin and stdout
    ds mcp check                               the mcp settings as judged, and what MCP is not (no store)

Settings come from ``--config`` or ``DS_CONFIG``. Exit codes: 0 success; 1 discrepancies found
(``--fail-on-diff``), a pair below the state asked for (``ds readiness --fail-unless``), or a table
pre-check with a pair as bad as asked, or cancelled (``ds precheck run --fail-on``); 2 usage,
input or configuration errors (one ``error:`` line each); 3 an internal error (a bug: its traceback is
printed, each exception by its type and never its message); 130 interrupted. Messages name files,
pointers and counts, never keys, payload values or credentials.

The CLI acts as ``local:<DS_OPERATOR>@<host>`` with every role, outside the role model: whoever holds
the DSN file and the tokeniser key can reach the store directly. With the auth
settings, the commands that write audit rows need ``DS_OPERATOR``; without them it defaults to the OS
account. It can neither request nor grant a four-eyes approval: under SSO ``ds run`` and ``ds compare``
run only an approved upload of the spec file's content. ``serve``, ``worker``,
``run`` and ``compare`` print the licence's warnings (a vendor build's; a build
without a vendor key needs no licence and has none to print); ``serve``, ``worker`` and
``run`` warn of a notification channel's unreadable secret. ``ds mcp`` serves an
agent as that principal cut to ``--role``, each tool call a request of its own.
"""

from __future__ import annotations

import typer

from deltasleuth.cli.approvals import approvals_app
from deltasleuth.cli.audit import audit_app
from deltasleuth.cli.common import _siem as _siem
from deltasleuth.cli.common import _Stream as _Stream
from deltasleuth.cli.keys import keys_app
from deltasleuth.cli.licence import licence_app
from deltasleuth.cli.mcp import _mcp_logs as _mcp_logs
from deltasleuth.cli.mcp import mcp_app
from deltasleuth.cli.notify import notify_app
from deltasleuth.cli.onboarding import _document as _document
from deltasleuth.cli.onboarding import adjudicate, llm_probe, measure, profile, score
from deltasleuth.cli.packs import packs_app
from deltasleuth.cli.prechecks import precheck_app
from deltasleuth.cli.readiness import readiness_app
from deltasleuth.cli.retention import purge_app
from deltasleuth.cli.runs import compare, report, run, runs
from deltasleuth.cli.samples import labellings, samples_app
from deltasleuth.cli.schedules import schedules_app
from deltasleuth.cli.service import _bindable as _bindable
from deltasleuth.cli.service import db_app, init, migrate, serve, worker
from deltasleuth.cli.specs import spec_app
from deltasleuth.cli.targets import target_app
from deltasleuth.cli.tokens import token_app
from deltasleuth.cli.users import auth_app, session_app, users_app

app = typer.Typer(
    name="ds",
    no_args_is_help=True,
    help="DeltaSleuth: compare what two sources hold, key by key, black-box: REST services, database tables,"
    " CSV and Parquet files.",
    pretty_exceptions_enable=False,
)

# The commands, then the groups, in the order ds --help lists them.
app.command()(init)
app.command()(migrate)
app.command()(serve)
app.command()(worker)
app.command()(compare)
app.command()(run)
app.command()(runs)
app.command()(report)
app.command()(profile)
app.command()(measure)
app.command()(adjudicate)
app.command()(score)
app.command("llm-probe")(llm_probe)
app.command()(labellings)
app.add_typer(spec_app, name="spec")
app.add_typer(token_app, name="token")
app.add_typer(session_app, name="session")
app.add_typer(users_app, name="users")
app.add_typer(auth_app, name="auth")
app.add_typer(purge_app, name="purge")
app.add_typer(keys_app, name="keys")
app.add_typer(approvals_app, name="approvals")
app.add_typer(audit_app, name="audit")
app.add_typer(packs_app, name="packs")
app.add_typer(licence_app, name="licence")
app.add_typer(samples_app, name="samples")
app.add_typer(notify_app, name="notify")
app.add_typer(readiness_app, name="readiness")
app.add_typer(schedules_app, name="schedules")
app.add_typer(precheck_app, name="precheck")
app.add_typer(target_app, name="target")
app.add_typer(mcp_app, name="mcp")
app.add_typer(db_app, name="db")
