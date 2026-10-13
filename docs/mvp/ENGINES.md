# Table sides on MySQL and Oracle: contract

This contract is binding. It builds table sides on MySQL and Oracle after readiness's
`014_readiness.sql`, and amends [TABLES.md](TABLES.md) and the documents of §17. It depends on no later
feature.

**Goal:** a table side may be a MySQL or Oracle table or view, in any pairing with each other, PostgreSQL
and a JSON service side. **Shape:** a small `Engine` seam where the runner calls today's PostgreSQL
connector. PostgreSQL keeps its statements; MySQL and Oracle share one statement composer over a dozen
primitives, so SQL Server later is one more primitive set. Each engine renders every value as text or
bytes and one Python canonicaliser turns it into the JSON value PostgreSQL's `to_jsonb` gives. Rows are
hashed in the engine when both sides are one engine, and in the worker across engines. No migration, no
licence fence, no new API route. Every claim stays *observed*.

**Observed** means run on 2026-10-01 against throwaway containers of the two local images
(`--pull never`, bundled clients only, removed afterwards), and again
for every fact a changed rule leans on. `mysql:8.4` reports 8.4.11;
`gvenzl/oracle-free:23-slim-faststart` reports 23.26.3.0.0 ("Oracle AI Database 26ai Free", AL32UTF8,
national character set AL16UTF16). **Not observed:** MySQL 8.0, Oracle 19c, RAC, TCPS against a real
listener, and an Oracle database in another character set (the image's scripts cannot set one; the
conversion is observed on `NVARCHAR2`, §8). Every driver call named here is
confirmed against the locked version.

## 1. Decisions (each one seam)

- **D1 Engines.** MySQL 8.0.17 and later (8.4 included) and Oracle 19c and later, with §17's wording that
  only 8.4.11 and 23.26.3 are observed. **MariaDB is out**, refused by its version string: no image
  of it was observed, and it differs where this design is sensitive (timeouts, JSON, collations). SQL Server is a seam.
- **D2 Drivers.** Oracle: `oracledb` in thin mode only (UPL-1.0 OR Apache-2.0; native asyncio, as psycopg
  3's; cp313 manylinux wheels for x86_64 and aarch64; its dependencies `cryptography` and
  `typing_extensions` are locked already). MySQL: `PyMySQL` 1.2.1 or later (MIT; one pure-Python wheel;
  §3 says why that version). It is synchronous: each session owns one thread, and it accepts a socket
  DeltaSleuth opened (§4). Rejected: aiomysql (a second package, parses on the event loop), asyncmy
  (compiled, one maintainer), and the GPL or LGPL drivers.
- **D3 PostgreSQL pairs are frozen.** A PostgreSQL-to-PostgreSQL pair, and a PostgreSQL table with a
  service side, keep today's run-time statements (the session's `SET`s, the probe, per-key reads, the
  scan's `DECLARE` and `FETCH`, the census, the frame, the null-key count, graph parity), stored plan,
  `plan_sha256`, census records and attestations. One statement did change for them: the catalogue
  statement that run creation and `ds compare` send (`_RELATION`, which `ds target check` sends too) also selects each
  column's declared type, which columns are enums and the relation's kind, and groups by that kind (§5);
  it needs no further privilege. The statements of 13 scenarios, captured before the seam
  and on this tree (`log_statement = all`), differ in no other text.
- **D4 Row hashing.** In the engine for a MySQL-to-MySQL or Oracle-to-Oracle pair (`SHA2`,
  `STANDARD_HASH`: §10); in the worker across engines, where no engine function gives the same bytes.
- **D5 One order, guarded.** Every side sorts in the key text's code-point order, and the merge refuses an
  out-of-order stream on every engine. Soundness rests on the guard, not on trust in a collation.
- **D6 Canonical rendering 1** (§9), stored in the plan and pinned by a golden corpus.
- **D7 Nothing is coerced silently.** Columns of different families are refused at run creation,
  naming the `typeMap` entry. Oracle's NULL against `''` elsewhere is a reported difference until the
  approved spec declares `emptyAsNull`, the one spec change (§9).
- **D8 Consistency** is recorded per side as `snapshot`, `unverified` or `none` and printed on the
  certificate; packs are still issued, with the warning, as for a listing's walk. A side that reads
  files ([FILES.md](FILES.md) §6, §11) records `fixed`: fixed files, never a snapshot.
- **D9 Refused on MySQL and Oracle sides:** graph parity (L2), `regex` key maps, `ILIKE`. On a file
  side these too, and `table.schema` and table pre-checks (FILES D10).
- **D10** The plan stores each side's engine and its server's major and minor version (`8.4`, `19.21`), so
  `plan_sha256` and a pack's `facts/plan.json` carry them; never a patch level, host, port or user.
- **D11** `ds draft` and a field picker do not exist: `ds profile` drafts service specs only (DEPLOY §7),
  and that stays. `ds target check` (§13) prints what a table spec's author needs.
- **D12 Every session goes to an address DeltaSleuth vetted** (§4). One path stays open and is accepted:
  an Oracle listener may redirect a session (RAC SCAN, shared server, a hostile listener) and the thin
  driver follows it, so an Oracle target is only as contained as its listener.
- **D13 TLS** is on unless `allow_unencrypted`, and a session that is not encrypted is refused before the
  password leaves. `tls_verify_name: false` (chain only, against a declared CA) exists because stock
  MySQL certificates carry no name (§4).

## 2. What is never claimed

No document, screen or certificate says: the databases are identical (only: no difference observed in the
compared columns of the rows and values the account reads, under rendering 1; redaction, masking and row
policies apply to what it reads); "read in one snapshot" for a MySQL view, a table outside InnoDB or a
resumed scan; "tested on" an unobserved version; that BENCHMARK's figures apply to another engine; that
DeltaSleuth cannot write to Oracle (the control is the account's grants); "encrypted" under
`allow_unencrypted`; that an Oracle session cannot be redirected (D12); or that key values never appear
in statement text (on MySQL they do, as hexadecimal literals).

## 3. Release gates

`uv add oracledb PyMySQL` under the lock's `exclude-newer` (2026-09-21, which admits PyMySQL 1.2.3), both
pinned exactly in `pyproject.toml`; a test refuses a PyMySQL below 1.2.1. Up to 1.1.x PyMySQL carried on
in plain text when the server offered no TLS; 1.2.0 raises 2026 instead, and 1.2.1 fixes an injection in
its escaping of bytes, which this design never calls (§10); both read in its source and changelog,
and the first proved by §16's stub server. `tools/release.py` `lacks` then requires both
drivers in the SBOM and pip-audit audits them, without a code change. The build confirms oracledb's wheel
adds no `*.libs` file to `vendored-libraries.txt`, or records dated decisions (RELEASE §8). The licence
gate is not built, so both licences are recorded by hand in architecture Appendix C.
`oracledb.init_oracle_client` is never called. The feature is not released before §15's measured figures
are in TABLES, DEPLOY and README.

§15's figures are measured and published (TABLES §13.9, DEPLOY §7, README), with what
was not measured said beside them. Both licences are in architecture Appendix C.2 and C.7, read from the
installed packages' metadata. **Still open before a release:** the check of `oracledb`'s wheel against
`vendored-libraries.txt` needs the release image. What is known: the lock gives `uv export` 71 packages, so `lacks` requires both drivers; the
macOS arm64 wheel that is installed holds four compiled modules and vendors no library; the manylinux
wheels are not inspected, and `tools/release.py build` refuses until every `*.libs` file it lists has a
dated `[[vendored]]` decision (RELEASE §7, §8). A test holds that `init_oracle_client` is called nowhere
(`test_tables_security.py`). **Closed on 2026-10-04** by the release rehearsal (RELEASE §7): the manylinux
wheels of `oracledb` 26.0.0, x86_64 and aarch64, hold no `*.libs` file; their four modules link libc and
libpthread alone, and `pymysql` 1.2.3 is pure Python there too.

The lock holds `oracledb==26.0.0` and `pymysql==1.2.3`. Two things PyMySQL 1.2.3 does
are worked around in `mysql.py`, each pinned by a test, to be looked at again when the
pin moves. Its `caching_sha2_password` full authentication without TLS (an account the server has not
cached, under `allow_unencrypted`) reads the server's last answer and returns nothing, which fails the
login it completed: a plugin handler in `auth_plugin_map` calls the same function and lets the lost answer
end the exchange. And it takes a read timeout only when it is built: the handshake runs under the connect
budget and the session's own timeout is set on the connection afterwards (`_read_timeout`).

Three more were found with a stub server and are bounded in `mysql.py`, each pinned by a
test (§14 point 6). Its read timeout bounds each read of the socket, so a greeting sent a byte at a time
held a connect for as long as its server liked (observed: 31.7 s under a budget of 1 s): the connect
budget is now a timer that shuts the socket down. It joins the 16 MiB parts of one packet without a
limit (observed: one row of 160 MiB took the process from 78 MiB to 686 MiB): a subclass of its
connection refuses a packet whose parts pass 80 MiB, as client code 2020. And parsing what no MySQL
server sends it raises exceptions that are not its own (`ValueError` from a greeting whose version is no
number, `struct.error` from one cut short, `RuntimeError` for a `LOCAL INFILE` request, which it does
refuse): §5 says what they become.

On `oracledb` 26.0.0 in thin mode (a test asserts the mode), three things are
to be looked at again when the pin moves. `connect_async` does not keep `tcp_connect_timeout` for an
address that never answers (observed: 75 s, the operating system's), so the whole connect runs under
DeltaSleuth's own 10 s budget, each address under its share of it (§4). Setting `call_timeout` on a connection that is closed crashes the
interpreter (observed: a segmentation fault), so it is set once, right after the logon, and never again.
And the driver puts a code of its own in the place of some of the server's (ORA-03113, 00028 and 03135 all
read `DPY-4011`), keeping the server's number beside it: §5 shows the server's.

Two more were found later. The driver's match of a certificate's subject (`ssl_server_cert_dn`)
keeps one value an attribute type and no order (observed against a Python TLS listener: with
`CN=crm-db,CN=Computers,DC=bank,DC=example` declared, the certificate of
`CN=laptop-17,CN=Computers,DC=bank,DC=example` was accepted, and with `CN=crm-db.bank.example,O=Bank`
that of `CN=evil,CN=crm-db.bank.example,O=Bank`): it is not used, and DeltaSleuth matches the subject
itself (§4). And `expire_time` turns `SO_KEEPALIVE` on but sets no idle time where the platform names it
`TCP_KEEPALIVE` (observed on macOS: the system's 7200 s, 75 s and 8 probes), while the driver shows no
socket: DeltaSleuth finds the connection's transport among the objects the connection holds
(`gc.get_referents`: the connection's protocol, its transport, asyncio's) and sets §4's options on its
socket. A test holds, on a live session, that the socket is found and what it is set to.

## 4. Targets (`core/spec/targets.py`, `core/types.py`)

A PostgreSQL target stays `dsn_ref`. A MySQL or Oracle target declares its endpoint, so the guard sees it:

```yaml
crm-legacy:
  engine: oracle                    # mysql | oracle
  host: crm-db.bank.example         # a DNS name or an IP literal
  port: 1521                        # default 3306 (mysql), 1521 (oracle)
  service: CRMPDB                   # oracle: a service name. mysql: `database: ledger`
  user: ds_reader
  password_ref: file:/run/secrets/crm_legacy_password
  tls_ca_ref: file:/run/secrets/crm_db_ca          # optional: else the system trust store
  tls_server_dn: "CN=crm-db.bank.example,O=Bank"   # oracle with a verified name: required
  allow_private_network: true
  rate_per_s: 50
  max_concurrency: 2
```

- **Schema.** `engine` requires `host`, `user`, `password_ref`, and `database` (mysql) or `service`
  (oracle); it excludes `dsn_ref`, `base_url`, `auth`, `capabilities` and `allow_insecure_http`. `service`,
  `database` and `user` match `^[A-Za-z0-9_.$#@-]{1,128}$`, so no connect descriptor, SID or alias can be
  written. `host` is an IP literal or a DNS name (labels of `[A-Za-z0-9-]{1,63}`, 253 characters; a host
  holding a colon that is no IP literal is `invalid_database_target` `host is a DNS name or an IP literal`);
  `tls_server_dn` matches `^[A-Za-z0-9 .,=@_:/+-]{1,512}$` (no quote, parenthesis or control character)
  and must parse as a name of RFC 4514 (`cryptography`'s `Name.from_rfc4514_string`: `CN`, `OU`, `O`,
  `DC`, `L`, `ST`, `C`, `STREET`, `UID` in upper case or a dotted OID, most specific first, no space
  after a comma), else T8.
  A member of an engine target on a target without `engine` is refused by the schema. Capabilities are
  `DATABASE_CAPABILITIES`.
- **TLS modes.** Default: the chain and the name are verified, MySQL against `host`, Oracle against
  `tls_server_dn` (T6), which the certificate's subject must equal: the same attributes, with the same
  values, in the same order, compared exactly (case included). `tls_verify_name: false`: the chain only, against `tls_ca_ref` (T7). The stock
  MySQL certificate is `CN=MySQL_Server_<version>_Auto_Generated_Server_Certificate` with no
  subjectAltName (observed), so it passes only this mode. It also carries no Authority Key Identifier,
  which Python 3.13's default `VERIFY_X509_STRICT` insists on (observed: `Missing Authority Key
  Identifier`), so the chain-only mode clears that flag; the default mode keeps it.
  `allow_unencrypted: true`: no TLS (T2).
- **TLS is enforced, not preferred.** One `ssl.SSLContext` serves both drivers. A MySQL server that offers
  no TLS is refused before authentication (PyMySQL's 2026, §3), so the password never meets a peer that
  stripped the capability; Oracle uses `protocol="tcps"`, TLS from the first byte. After login the
  session's own report is a second check (`Ssl_cipher` not empty; `SYS_CONTEXT('USERENV',
  'NETWORK_PROTOCOL')` is `tcps`, observed `tcp` on a plain listener), else P1. The plan records `tls` as
  true or false. PyMySQL runs with `local_infile=False`.
- **`Target`** gains `database: DatabaseTarget | None` and the property `engine` (`"postgresql"` for
  `dsn_ref`). `TargetRegistry.live`, `rules._targets` and `compare.connect` test `engine` where they test
  `dsn_ref`.
- **Secrets.** `password_ref` and `tls_ca_ref` only, resolved against the targets file and read at every
  connect. The password is a driver argument, never part of a connection string. Never read: environment
  variables, `~/.my.cnf`, `tnsnames.ora`, `sqlnet.ora`, wallets. A CA file that holds no PEM certificate
  (an empty one would otherwise mean the system trust store) is a `SecretError` naming the file.
- **SSRF at load:** a literal loopback or private host needs `allow_private_network` (T4); a literal
  link-local, unspecified, multicast or metadata host is refused (T5).
- **SSRF at every connect** (new `netguard.vetted(host, port, allow_private)`): `system_resolve`, then
  the address rules of `core.notify.refusal` on every answer, split out as a predicate that each caller
  words for itself (§12 G1, G2; `core.spec.targets.address_rule`, which `core.notify` imports: the other
  way round is an import cycle); one refused answer raises `UnsafeTargetError`. The vetted addresses are
  tried in order inside the 10 s connect budget, each given an equal share of what is left of it when its
  turn comes (of two addresses the first has 5 s, the second the rest), so an AAAA answer without a
  route, or one that never answers, falls back to IPv4. On Oracle an address's share covers its connect
  and its logon.
- **An IPv6 address that stands for an IPv4 one is judged as that one,** at load and at every connect:
  IPv4-mapped (`::ffff:0:0/96`), IPv4-translated (`::ffff:0:0:0/96`), the well-known NAT64 prefix
  (`64:ff9b::/96`) and the IPv4-compatible form (`::/96`). On a DNS64 network a name whose A record is
  10.1.2.3 answers `64:ff9b::a01:203`, which Python's `is_global` calls global (observed): it is G1,
  and `64:ff9b::a9fe:a9fe` (169.254.169.254) is G2. A network-specific NAT64 prefix cannot be known and
  is not judged. The rule is `address_rule`'s, so the notification guard has it, and a service target's
  resolver (`netguard.is_forbidden_address`) now asks the same predicate.
- **Pinned on every path.** MySQL: DeltaSleuth opens the socket to the vetted address and hands it to
  PyMySQL (`defer_connect`, `connect(sock)`), TLS still verifying the declared name. PyMySQL wraps that
  socket for TLS and keeps the wrapper to itself, so the session holds a second handle of the same socket
  (`dup`): shutting it down is what a close does (§6). Oracle: the driver's
  `host` is always the vetted address, never the name, in all three TLS modes, and the driver matches no
  name (`ssl_server_dn_match=False`, `check_hostname` cleared): it dials an address, and its own match
  of a subject is not exact (§3). That is why an Oracle target whose name is verified must declare
  `tls_server_dn`: DeltaSleuth compares the peer certificate's subject with it for equality
  (`cryptography`'s `Name`) inside the handshake, through the context's `sslobject_class`. A certificate
  of another CA and one of another subject both end the handshake, before the driver sends a packet
  (observed against a Python TLS listener, for subjects that repeat an attribute type, hold a comma in a
  value, add an attribute, or differ in order or in case). The redirect of D12 is the one unvetted path.
- **Sockets.** A session's socket gets `SO_KEEPALIVE` with idle 60 s, interval 10 s and 6 probes
  (`TCP_KEEPIDLE` or `TCP_KEEPALIVE` by platform), on both engines: asserted on the socket a MySQL
  session is opened with and on a live Oracle session's, on the platform the tests run on (macOS here;
  Linux is not observed). The MySQL socket also gets
  `TCP_NODELAY`: PyMySQL sets neither on a socket it is handed. On Oracle the three options are set
  after the logon on the socket of the driver's transport, found as §3 says; where it is not found the
  driver's `expire_time=1` alone stands. Oracle also gets `tcp_connect_timeout=10` and `retry_count=0`,
  and its whole connect (every address, the logon, the settings) runs inside one 10 s budget of
  DeltaSleuth's own (§3). A MySQL connect has one budget too: the addresses, the handshake and the
  session's settings end with it, whatever the server sends and however slowly (§3).

## 5. The seam (`deltasleuth/connectors/tables/`)

- `engine.py`: the `Engine` protocol, `ENGINES`, `of(target)`, `named(side_plan)` (a plan without `engine`
  is PostgreSQL), `Relation`, the neutral errors. `ENGINES` is filled last in the module, after the
  adapters it imports, because each adapter imports the module's types; so `connectors/postgres/__init__`
  never imports its own `engine`, and PostgreSQL's `UNFIT` lives in `postgres/session.py`.
- `plan.py`, `connector.py`, `keys.py`: today's `postgres/` modules moved (`git mv`), engine-neutral.
- `compose.py`: §10's statements, written once over an engine's primitives (`ident`, `relation`,
  `literal`, `key_text`, `order`, `exact`, `after`, `render`, `lob`, `oversize`, `row_hash`, `first`,
  `selected`, `prefix_pad`, `like`, `arms`). `exact` writes an order expression equal to a key's part and
  `lob` says whether a declared type may hold a large value; `row_hash(side,
  columns)` writes §10's hash of a side's engine columns. A builder returns its statement and
  the values it binds (none on MySQL). `literal` takes its
  column's declared type (Oracle compares a `DATE` as a `DATE` and tells `CHAR` from `NCHAR`), `exact`
  answers None for a part no key text can be (Oracle's `''`), `arms` says how many arms a statement of so
  many keys is written with (§10), and what reads the composed statements' rows is here too,
  for both engines: `found` (per-key rows), `digested` (a scan's batch), `stratified` (a
  frame's).
- `render.py`: families, canonical values, `document()`, `parsed()` (PostgreSQL's row), `digest()`. The
  table of a declared type's class is its engine's (`mysql._CLASSES`, `postgres/engine._CLASSES`).
- `mysql.py`, `oracle.py`: session, catalogue query, primitives, error codes (`mysql.py` with its
  socket, its thread and its cancel, `oracle.py` with its typed binds, its catalogue and its synonyms).
- `connectors/postgres/` keeps `session.py` and `queries.py` and gains `engine.py`, an adapter; `_RELATION`
  adds `format_type` and which columns are enums, so PostgreSQL columns get a class in a cross-engine
  pair, where its adapter also gives each row the canonical form (§9) and the worker's digest (§10).

```python
class Engine(Protocol):
    name: str   # postgresql | mysql | oracle; request_url is <name>://<target_id>/<relation>
    arms: int   # keys per per-key statement: 200, 200, 50
    unfit: tuple[type[BaseException], ...]   # a key the column cannot hold (§8)
    fold: Callable[[str], str] | None   # an unquoted name's case: lower, None (any case matches), upper
    hides: bool   # its catalogue lists only columns the role holds a privilege on: False, True, False
    grant: str    # the privilege a role reads a relation by (P12): SELECT, SELECT, READ
    async def connect(self, target, *, timeout_s=0) -> Session
    def snapshot(self, target, *, timeout_s=0) -> AsyncContextManager[Session]
    async def server(self, session) -> dict   # version (full), charset, tls: `ds target check` alone
    def release(self, session) -> str         # the server's major and minor version, as a plan's (D10)
    async def describe(self, session, schema, name, *, side=None) -> Relation | None
    async def probe(self, session, side) -> None
    def large(self, side) -> bool             # a per-key row may hold a large value (§6)
    async def rows(self, session, side, asked) -> list[Row]  # position, document (or why none), selected
    async def scan(self, session, side, after) -> AsyncIterator[list[tuple]]    # (key parts..., digest)
    def census(self, session, side) -> AsyncIterator[list[tuple]]         # (key parts...)
    def frame(self, session, side) -> AsyncIterator[list[tuple]]          # (key parts..., strata JSON)
    async def null_keys(self, session, side) -> int
    # Table pre-checks (PRECHECKS.md §5):
    sum_digits: int | None   # the digits its sum of numbers holds: None, 65, 38
    empty_null: bool         # it stores '' as NULL (D7): False, False, True
    def built(self, column, cls, declared, key) -> str | None  # a key's key text, else its render; None on PostgreSQL
    async def relations(self, session, schema) -> list[tuple[str, str]]   # each readable table's and view's name, kind
    async def figures(self, session, side, wanted) -> list[str | None]    # one statement, one row of texts
    async def duplicates(self, session, side) -> tuple[int, int]          # repeated keys, the rows holding them
    # The files engine (FILES.md §6):
    retry_alone: bool        # a timed-out group is re-read key by key: True, True, True, False (files)
```

**The files engine's amendments** ([FILES.md](FILES.md) §6). `describe` takes a keyword `side`, which the files engine
alone reads: the side's `SideConfig` when a run is planned (its files are pinned and described there),
the stored plan afterwards (a stat check, then the relation as planned); the other engines ignore it, and
a call without it (a table pre-check's) is refused on a file side. `retry_alone` is false for `files`,
whose every statement is a pass over the files. `relation(side)` is the name alone for a side without a
schema. The files engine's `name` is `files`, its `arms` 200 and its `unfit` empty: a key no column of
the file can hold is on no row.

`Relation` is `(schema, name, columns[name, declared, cls, readable, likeable, keyable, lob, nullable,
size], facts{kind, key, server, consistency, tls, charset})`: what the table pre-checks and schema guard
read (`nullable`, `size` and `key` were added for them: PRECHECKS.md §5). `kind` is `table`
or `view` (PostgreSQL also says `materialized view` and `foreign table`), read by the catalogue
statement and printed by `ds target check` alone; the plan stores none of it. `cls` is None for a
type no plan reads (P7); `keyable` says the column's key text is one every engine renders alike (§8), so
the neutral plan words P6 for every engine, PostgreSQL's side of such a pair included; `lob` says it may
hold a large value (§6), and Oracle's facts add `national`, its national character set: both are what
the plan chooses §10's engine columns by. `fold` and `hides` are what the plan needs of an
engine's catalogue to find a column and to word P12. A per-key `Row`'s document is its
JSON text, or the `ValueError` that left it without one (a value not of its class's form, a row past §6's
bound), which the connector records as `unconvertible` or `resource_limit`.

**`scan` is awaited** (`census` and `frame` start at their first fetch): when it
returns, its statement is sent. PostgreSQL takes a `REPEATABLE READ` snapshot at the first statement, the
scan's `DECLARE`, so both sides stay pinned when they are opened, milliseconds apart, as before the seam
(D3). As a plain generator, side b's snapshot would wait for side a's first fetch, which is its whole sort.
An engine fetches in batches of its own `BATCH` (10,000 rows, PostgreSQL's fetch size); `scan.BATCH` is
the checkpoint's alone, since the protocol passes no size and a connector cannot import the runner.

**Errors.** `mysql.py` and `oracle.py` let no driver exception out: each becomes a `TableError(code)`
raised `from None`, holding only a code (`ORA-01555`, `MySQL 1142`, `DPY-4024`, or an exception's class
name such as `gaierror`), because MySQL 1525 and ORA-03302 quote values (observed). On MySQL that is
whatever the driver raises in the session's thread, of any class: a server that is no MySQL server makes
PyMySQL raise `ValueError`, `struct.error` (whose class name is `error`), `IndexError` or `RuntimeError`
with the server's bytes in the text (§3), and each becomes `TargetUnreachable(<class name>)` with the
gloss `the answer is not a MySQL server's`. What DeltaSleuth's own code raises while it shapes a fetched
batch or a group's rows in that thread stays what it is. Subclasses:
`KeyUnfit`; `TargetUnreachable`, with `StatementTimeout` under it; `SnapshotLost`; `TargetAuthRefused`;
`TargetTempSpace`; `TargetDenied`; `TargetRefused` (any other server code); `EngineChanged`; and, of the
files engine ([FILES.md](FILES.md) §10), `FilesChanged`, `RowsMoved` and `ReaderDied` (the file reader's
process ended three times while one side's files alone were being read, [READER.md](READER.md) §9: in
none of `UNREACHABLE`, `STALLS` and `drift.DENIED`, so a scan and a per-key read fail the run with it). `NotReady`, a `ValueError`
holding a closed `code`, is what a file side's plan refuses an export that is not ready with (FILES §12).
`KeysOutOfOrder` is a `ValueError`. `UNREACHABLE = (psycopg.OperationalError, TargetUnreachable)`,
`TIMEOUT` and `ERRORS` (`psycopg.Error`, `TableError`: what a probe is refused with) keep each caller's
`except` one line, and `code(exc)` is what any of them may show: a `TableError`'s code, else the
exception's class name, as PostgreSQL's errors show today. `cycle.failure` shows a `TableError` as
`<class>: <code>` and a guard's `UnsafeTargetError` as `UnsafeTargetError: <its text>` (an address, never
a key or value). A `run.failed` notification carries the class alone (`notify.error_type` sends what
stands before the first `: ` when that has a name's form, so `TargetTempSpace`, `UnsafeTargetError`;
ALERTS §2.1).
A `TableError` may carry a `gloss`, what its code means where a refused connect says it (P1). Also in
`engine.py`: `Refusal` (a `ValueError`: what an engine does not plan, P2 to P11, worded
without its side, which the plan adds), `REFUSED` (what a connect, or the guard before it, fails with),
`unreachable(exc)` (the neutral error of a socket's, a resolver's or TLS's failure, by its class),
`tls_context(database)` (§4's one context for both drivers) and `planned(side, plan, target)` (`named`,
refusing a target whose engine is no longer the plan's: F6).

**Classified by origin first** (§11's table lists the codes):
1. A listed code has its class. An Oracle code is the server's number wherever the driver's exception
   carries one, also under a `DPY-` code of the driver's (§3): `ORA-03113`, not `DPY-4011`.
2. A failure with no server code is `TargetUnreachable`: `OSError` (so `socket.gaierror`, a refused or
   reset connection and `ssl.SSLError`, which reach DeltaSleuth raw now that it opens the MySQL socket),
   `TimeoutError`, `EOFError`, the drivers' `InterfaceError`, MySQL client codes 2000 to 2999, any
   exception of another class PyMySQL raises, and any `DPY-` code not listed. The thin driver wraps a failed connect in `DPY-6005`: its cause is classified
   in its place (a socket's or TLS's error by its class, the listener's refusal by the driver's code for
   it, `DPY-6001` for an unknown service, which carries no server number).
3. Any other server code is `TargetRefused`. It never fails a run on first sight (§11).

**Runner edits.** `scan.py` `_Stream` and `_snapshot` read `engine.scan` and gain the guard (§8);
`_digest` moves to `render.digest` (each engine digests its fetched batch, so a scan row is (key
parts..., digest); a row without one carries the marker `unconvertible:` or `resource_limit:`, which
`_Stream` completes with its side, so it never matches). MySQL, Oracle and the PostgreSQL side of a pair
with either digest a batch off the event loop; a PostgreSQL pair's typeMap columns are digested on it,
where `_Stream._digest` ran before the seam (§15). `census._count` and
`sample._Frame._table` read `engine.census` and `engine.frame`. `runs.create_run` and `compare_one` call
`tables.plan`. `graph.py` stays on `connectors.postgres`. `where.render`'s `literal` callback gains its
column. `worker._scan` counts stalled passes (§11). `ds serve` gains `--no-worker` (`create_app(settings,
worker=False)` exists).

**The seam also has** `Engine.release(session)` (the major and minor version of the
server a session met, which `describe` stores and each scan pass, census and frame records: §13),
`Engine.large(side)` (§6: `compose.large`; PostgreSQL answers false), and `engine.keep_alive(sock)` with
`KEEPALIVE`, §4's options for both engines' sockets. MySQL's version numbers are read from its version
string by one pattern (`_numbers`), so no other text of a greeting reaches a plan's `server`. The plan
words what a database answers after the connect (§12 P14: `_described` and `preflight` catch `ERRORS`),
and a refused connect is an `Unconnected`, a `ValueError` whose message is P1 and whose `brief` names the
side and the target alone (§14 point 9).

**Table pre-checks** ([PRECHECKS.md](PRECHECKS.md) §5) added to the seam, on every engine:
a column's `nullable` (None for a view's column on every engine, and for a PostgreSQL materialized view's)
and `size` (`engine.sized(cls, declared)`: `("digits", p, s)`, `("characters", n)`, `("octets", n)` for an
Oracle text declared in bytes, `("fraction", n)`; None when the type declares none), the fact `key` (the
primary key's columns in its order, or None; MySQL's from `STATISTICS`, which lists a key column the account
may not read too), each read by the catalogue statement `_RELATION` still in one statement; the attributes
`sum_digits` and `empty_null`; `built` (what its statements read a planned column of a declared type as: a key
column's `key_text`, any other's `render`, None on PostgreSQL, whose statements name no declared type: the schema
guard compares two declared types', PRECHECKS §7); and `relations`, `figures` and `duplicates`.
`compose.py` gained `figures`, `duplicates` and `figured` over three more primitives, `rendered(value, cls,
declared)` (what `render` writes of any expression of the column's type: a minimum's and a maximum's, and a
sum's as of a decimal, so that MySQL's sum of a `bit`, a `DECIMAL`, is not clipped by the `UNSIGNED` cast of
its column), `length` and `distinct`; PostgreSQL's `relations` lists no partition, its partitioned table
reading its rows, as MySQL's and Oracle's catalogues list a partitioned table once; Oracle's
`_Session.query` takes `decimals`, to fetch numbers as `Decimal`. `plan.py` gained `issues`, `resolved`
and `planning` (`plan()` is `planning().plan`, its plan and `plan_sha256` byte for byte the same), and
`described` and `probed` for a session the caller holds. `tables/figures.py` is what a pre-check reads of
one side (`classed`, `read`, `failure`). MySQL 1054, a column that is gone, joined the denied class (§11).

**For `ds target check`** the seam has `Engine.server(session)`, the session's own account of its server (its full version,
its character set, whether it is encrypted; outside AL32UTF8 Oracle's also proves that the role may
execute `UTL_I18N`, P5), the fact `kind` (PostgreSQL's catalogue statement reads `relkind`, Oracle's
`ALL_VIEWS`, MySQL's had `TABLE_TYPE`), `OBSERVED` (the server version each engine was observed on) and
`plan.preflight(target, table)`, which shares the plan's connect and its wording of a relation that is
missing or not readable (`_connected`, `_described`, each worded for `side a` or for `target <id>`).

**For engine hashing** (§10) the seam has `row_hash` in both primitive sets, `compose.engine_columns` (the compared columns
a plan leaves out of `digest`, in the order of their canonical names) with the hash in `scan`, in `probe`
and in `digested`, `Column.lob`, Oracle's `national` fact (one more value of the session's read-back
statement) and the plan's choice of the worker's columns (`plan._worker_columns`). A
connect refused at the plan's second session, the probe's, is worded as P1 to P4.

## 6. Sessions

| | MySQL | Oracle |
|---|---|---|
| Every session | `SET NAMES utf8mb4`; `sql_mode = ''`, `time_zone = '+00:00'`, `wait_timeout`, `net_write_timeout` and `net_read_timeout` 86400, `max_sort_length = 8388608`, `max_execution_time` the timeout; `SET SESSION TRANSACTION READ ONLY`; autocommit, no value converters | `ALTER SESSION SET NLS_SORT = BINARY NLS_COMP = BINARY NLS_CALENDAR = 'GREGORIAN' TIME_ZONE = 'UTC' NLS_NUMERIC_CHARACTERS = '.,'`, all five read back (`nls_session_parameters`, `SESSIONTIMEZONE`): a logon trigger may override one (P4); the same statement reads the session's protocol, its `CURRENT_SCHEMA` and the database's character set and national character set (§10); `call_timeout` |
| Read-only | every transaction of the session is read-only (observed) | no session switch exists: every read, per-key batches included, runs inside `SET TRANSACTION READ ONLY` ... `COMMIT` (ORA-01456 on a write, observed): two more round trips a statement |
| Grants | `SELECT` on each relation (column grants honoured) | `CREATE SESSION`, and `READ` on each relation (it refuses `FOR UPDATE`, observed); no catalogue role |
| Snapshot | `REPEATABLE READ`, `START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY` | one `SET TRANSACTION READ ONLY` for the pass |
| `consistency` | `snapshot` for an InnoDB base table; `unverified` for a view (its `ENGINE` is NULL, observed); `none` for another storage engine (a MyISAM table accepts the statement unpinned, observed) | `snapshot` |
| Snapshot lost | 1412 | ORA-01555, ORA-08176, ORA-01466 (observed for up to 2 s after DDL on the table, and for a table created after the pass began; ORA-08176 observed when a table got its first row, and so its segment, after the pass began) |
| Cancel | best effort `KILL QUERY` from a second short session (when a statement runs or its rows still come), then `shutdown(SHUT_RDWR)` and close, which wakes a thread blocked in `recv`; an unbuffered cursor is never drained | `cancel()` when a statement runs (it ends with ORA-01013, observed at once), then close, waited for 10 s at most |

- `wait_timeout`: an idle session past the stock limit answers its next statement with 4031 (observed).
  `net_write_timeout`: side a's stream idles while side b sorts, and the stock 60 s would drop it.
- **Timeouts:** per-key reads `execution.total_timeout_s`; census and frames 3600 s; scans none, as today.
  A session with a statement timeout also has a client read timeout 30 s above it (PyMySQL
  `read_timeout`; `call_timeout` is the client's own on Oracle, each round trip: a statement past it ends
  as `DPY-4024` and the session stays usable, observed; a PL/SQL call that cannot be interrupted ends as
  `DPY-4011`, the connection closed). A scan has no statement timeout and relies on §4's keepalive
  probes: by the options set, a flow a firewall dropped silently is given up after 60 s idle and 6
  probes 10 s apart, 120 s, as `TargetUnreachable`, and the scan resumes. The options are asserted; the
  drop itself is not made by a test (§16).
- **A scan ends when its run or its worker stops.** The worker's sweep cancels the scan task of a run
  that is no longer `running` (paused, cancelling, cancelled, stopped or failed), and a worker that is
  stopped cancels every scan: `ds serve` on its shutdown, `ds worker` and the worker inside `ds run` on
  SIGTERM and on Ctrl-C. The task's unwinding closes both sessions as the Cancel row says. Observed on
  MySQL, on a view that takes half a minute to read: both statements and both sessions gone about a
  second after a cancel and after a pause (asserted under 5 s), and both statements gone at the exit of
  a terminated `ds worker` and `ds run`. Before, the scan
  learnt of its run's state at its next checkpoint, after the sort, and SIGTERM ended `ds worker` where
  it stood.
- **LOB columns** are those declared `TINYTEXT` to `LONGTEXT`, `TINYBLOB` to `LONGBLOB` or `JSON` (MySQL),
  `CLOB`, `NCLOB`, `BLOB` or `JSON` (Oracle).
- **Memory is bounded in bytes.** The `oversize` primitive keeps a large value off the wire: a row whose
  LOB values total more than 16 MiB (`LENGTH`: bytes on MySQL and for a `BLOB`, characters for a `CLOB`;
  observed) is selected with its LOB columns NULL and a flag, digests as `resource_limit:<side>` and reads
  per key as `resource_limit`. MySQL reads its unbuffered cursor row by row and closes a batch at 10,000
  rows (its `BATCH`, §5) or 64 MiB fetched. Oracle sets `arraysize` (and `prefetchrows`) to 64 MiB over the
  declared row width, from 1 to 10,000, a row with a LOB column counting 16 MiB more: 4 rows or fewer. A
  column's width is four bytes a declared character or byte, 64 at least. LOBs are fetched as text and
  bytes (`fetch_lobs=False`), never as locators; an empty `CLOB` or `BLOB` reads as NULL, as `''` does.
  - **The PostgreSQL side of a pair with another engine**, where a row comes with every compared column
    (§10): its first fetch is 100 rows, and each later one as many as hold 64 MiB at the width of the
    widest row of the fetch before it, from 1 to 10,000. So rows much wider than all those of the fetch
    before them are fetched once at the earlier size, and PostgreSQL has no `oversize` guard: a row is
    fetched whole, whatever it holds. Observed: 600 rows of 100 KB came in one batch before, and under a
    4 MiB budget in fetches of 100, then 41. A PostgreSQL pair keeps its fixed 10,000 (D3).
  - **A per-key group** is 200 keys (TABLES §12), or 4 when a compared column of a MySQL or Oracle side
    is a LOB column (`Engine.large`; the worker's `LOB_BATCH`, the group being what is held, not a
    statement; beside a file side the lease stays 200 keys and such a side is read 4 at a time inside
    it: [FILES.md](FILES.md) §12): each key answers with at most two rows of at most 16 MiB of LOBs, so such a group holds
    at most 8 rows a side, with their documents and bodies, where 200 keys held 400. Observed with
    `tracemalloc`: 12 keys of 2 MiB rows a side peaked at 73 MiB in groups of 4 and at 197 MiB in one
    group. At most `execution.concurrency` groups of a run are in flight. A MySQL group's documents
    are built in the session's thread, off the event loop. PostgreSQL classes no type a LOB: its side
    alone leaves the group at 200.
  - **A MySQL packet** is refused past 80 MiB (64 MiB and a row's 16 MiB of LOBs; client code 2020,
    unreachable): the driver joins a packet's parts before any bound above is consulted (§3).
- **Sessions per run:** one per side for per-key reads, one more per side while it scans, one for a census
  or frame, two short ones at run creation (the catalogue, then the probe, as PostgreSQL's). No pool.
  Sessions and a target's budget (`rate_per_s`, `max_concurrency`: `ctx.gates`) are a process's own.
  Under `ds serve --no-worker` the API process's one-key compares, key listings and run creation
  therefore have a budget and sessions beside the worker's, and a target can see up to twice both
  limits; DEPLOY §7 and RUNBOOK say so and tell a site to size `max_user_connections` and
  `SESSIONS_PER_USER` for both processes (read from the code, not measured).
- **A MySQL scan's statement is sent when `scan` is called** and its first answer awaited at the first
  fetch: the server answers only once it has sorted, and side b's snapshot must not wait for side a's sort
  (§5). Its snapshot is the one `START TRANSACTION WITH CONSISTENT SNAPSHOT` took. An Oracle scan does the
  same (its statement is a task started by `scan`); its moment is its `SET TRANSACTION READ ONLY`'s.
- **MariaDB and a MySQL before 8.0.17 are refused at every connect** (P2), from the greeting's version.

## 7. Plan (`tables/plan.py`)

- **Version.** A MySQL `VERSION()` holding `MariaDB`, or below 8.0.17, is refused (P2); Oracle below 19 (P3).
- **Relation.** As spelled, else the one name equal ignoring case (MySQL) or upper-cased (Oracle), from
  `information_schema.COLUMNS` and `TABLES`, or `ALL_TAB_COLUMNS`. A column resolves the same way (two
  MySQL columns cannot differ by case alone). A name the catalogue's character set cannot hold (an emoji:
  1267 from `information_schema`, observed) names no relation. Oracle follows one synonym level
  (`ALL_SYNONYMS`) and stores the real owner and name; a synonym over a database link is refused. The
  schema's own synonym is taken before a public one, and a public one only when the spec names no schema.
  `schema` omitted: the target's `database` (MySQL), the session's `CURRENT_SCHEMA` (Oracle). Canonical
  names stay lower-cased, so Oracle `EMAIL` pairs with PostgreSQL `email`. `ALL_TAB_COLUMNS` lists a
  column declared `INVISIBLE`, after the others (it is read by name, and discovered, as any column), and
  not the columns the server adds for itself (a function-based index's; observed).
- **Grants.** MySQL lists only columns the account holds a privilege on (observed): readable means
  `select` in `COLUMNS.PRIVILEGES`. Oracle lists a table reachable by INSERT alone (observed): readable
  means the zero-row probe raises no code of §11's denied class (ORA-41900 observed on 23.26.3; 19c is
  expected to say ORA-01031 or ORA-00942, not observed).
- **When the cross-engine check runs.** Whenever a table side's engine is MySQL, Oracle or `files`, a
  file-to-file pair included ([FILES.md](FILES.md) §6): families (P9), `other` (P8) and key texts (P6). A
  PostgreSQL side of a pair with a file side stores `classes`, `declared`, `tls` and `consistency:
  snapshot`, and the plan `rendering: 1`, as beside MySQL or Oracle. A file side's classes are FILES
  §8's (a Parquet column by the type DuckDB reads it as, a CSV field `text`, or `char` with `padded`).
- **Classes**, from the declared type:

| Class | PostgreSQL | MySQL | Oracle |
|---|---|---|---|
| `integer` | smallint, integer, bigint | integer types, `TINYINT(1)`, `YEAR`, `BIT` | `NUMBER(p,s)` with s ≤ 0 |
| `decimal` | numeric | `DECIMAL` | other `NUMBER`, `FLOAT(n)` |
| `float`, `float32` | double precision, real | `DOUBLE`, `FLOAT` | `BINARY_DOUBLE`, `BINARY_FLOAT` |
| `text` | text, varchar, uuid, enums | `VARCHAR`, `TEXT` types, `ENUM`, `SET` | `VARCHAR2`, `NVARCHAR2`, `CLOB`, `NCLOB` |
| `char` | character(n) | `CHAR` | `CHAR`, `NCHAR` |
| `boolean` | boolean | none | `BOOLEAN` (23) |
| `date` | date | `DATE` | none |
| `timestamp` | timestamp | `DATETIME` | `DATE`, `TIMESTAMP` |
| `instant` | timestamptz | `TIMESTAMP` | `TIMESTAMP WITH [LOCAL] TIME ZONE` |
| `time` | time | `TIME` | none |
| `bytes` | bytea | `BINARY`, `VARBINARY`, `BLOB` types | `RAW`, `BLOB` |
| `json` | json, jsonb | `JSON` | `JSON` (21c) |
| `other` | any other type | none | `INTERVAL`, `ROWID`, `UROWID` |
| refused (P7) | none | spatial, `VECTOR` | `LONG`, `LONG RAW`, `BFILE`, `XMLTYPE`, object, collection and spatial types |

  A refused type is refused wherever the plan would read it, discovered columns included: never dropped.
  Oracle's declared type is written out from the catalogue: `NUMBER`, `NUMBER(10)`, `NUMBER(10,2)`,
  `NUMBER(*,0)` (an `INTEGER`), `FLOAT(126)`, `VARCHAR2(120 CHAR)`, `VARCHAR2(100)`, `NVARCHAR2(50)`,
  `RAW(16)`, and the catalogue's own text for the rest (`TIMESTAMP(6) WITH TIME ZONE`).
- **Families:** text (`text`, `char`); number (`integer`, `decimal`, `float`, `float32`); every other class
  alone. A `typeMap` entry sets its side's family: `string` text; `integer`, `decimal` number; `boolean`,
  `date`, `json`, `bytes` their own; `timestamp` one of its own (its `Z` form equals only another mapped
  timestamp).
- **Cross-engine check**, when both sides are tables and one is MySQL or Oracle: a compared column's two
  families must be equal (P9), `other` never crosses engines (P8), and a key column's two sides must
  render one text (§8, P6): two classes whose key texts are never equal are refused, as two number
  scales are.
- **Filter literals** are checked in Python against their column's class (P11): neither engine's zero-row
  probe refuses a wrong-typed literal (observed: MySQL `int_col > 'abc' LIMIT 0` raises nothing).
- **Probe:** `LIMIT 0` (MySQL) or `WHERE 1 = 0` (Oracle) proves names, privileges and operators at parse.
- **Steps** ([PRECHECKS.md](PRECHECKS.md) §5). The plan is made in steps: each side's names and
  grants; the pair's counterparts; what each side compares; with a MySQL or Oracle side the classes read,
  then the pair's keys and families. `plan.issues` lists every refusal of the first step that refuses (a
  table pre-check's `refused` line), `plan.resolved` raises the first, and `plan.planning` describes every
  table side before it plans, so a side that cannot be described is refused before another side's column.
  The plan and its messages are otherwise unchanged.
- **Stored form.** A pair with a MySQL or Oracle table side adds to each table side `engine`, `server`
  (D10), `consistency`, `tls`, and `classes` and `declared` by column (keys, compared, filter and strata
  columns, e.g. `"OPENED": "timestamp"` and `"OPENED": "DATE"`); `digest` is every column the worker
  hashes (§10), the same list on both sides, and the engine hashes the compared columns it leaves out:
  none across engines, so there `digest` is every compared column; the top level adds `rendering: 1`. A PostgreSQL side of such a pair stores `tls` from its
  connection's `ssl_in_use` and `consistency: snapshot`. A run whose target's engine is no longer its
  plan's fails (F6).

## 8. Keys and one total order

**Key text**, equal to PostgreSQL's `::text` for the same declared value:

| Class | MySQL | Oracle |
|---|---|---|
| `integer` | `CAST(k AS CHAR)`; a `ZEROFILL` column through `CAST(k AS UNSIGNED)` first (`00007` otherwise, observed) | `TO_CHAR(k, 'TM9')` (38 digits exact, observed) |
| `decimal` | `CAST(k AS CHAR)` (scale kept, as PostgreSQL) | `NUMBER(p,s)`: a fixed mask built from p and s, `FM99999990.00` for `(10,2)`, which prints `1.50`, `2.00`, `-0.50` (observed; TM9 prints `1.5`, `2`, `-.5`). Unconstrained `NUMBER`: TM9 for a whole number below 1E38, else NULL |
| `text` | `CONVERT(k USING utf8mb4)` | `k` |
| `char` | the same (the server strips the padding) | `RTRIM(k, ' ')` |
| `date` | `CAST(k AS CHAR)` | none |
| `timestamp` without fractional seconds | `CAST(k AS CHAR)` | `TO_CHAR(k, 'YYYY-MM-DD HH24:MI:SS')` (`DATE`, `TIMESTAMP(0)`); NULL for a year before 1 |
| `bytes` | `\x` and lower-case hex | `\x` and `LOWER(RAWTOHEX(k))` |

An Oracle `DATE` holds years before 1, and `YYYY` prints 44 BC as AD 44 (§9): such a key would pair with
its AD twin, so it has no key text and its row is counted in `skipped_x`, as an unconstrained `NUMBER`
that is no whole number is.

- **Number keys pair only at one scale.** When the other side is a table, a number key's two sides must
  declare the same scale (an `integer` class is scale 0), else P6 names both declared types: `1` never
  pairs with `1.00`, and every row would be one-sided on both sides without a refusal. An unconstrained
  Oracle `NUMBER` (the usual `ID NUMBER`) counts as scale 0: a row whose key is not a whole number has a
  NULL key text and is counted in `skipped_x`, as a key its key map does not match. A PostgreSQL
  `numeric` without a scale declares none and is refused in such a pair.
- **Key classes pair only where their texts can be equal.** When the other side is a table, a `date` key
  against a `timestamp` key is refused (P6, naming both declared types): `2024-01-01` is never
  `2024-01-01 00:00:00`, which is what a date key reads as once it is an Oracle `DATE` or a MySQL
  `DATETIME`, and every row would be reported missing on both sides (observed before the refusal: 10
  identical rows, 20 one-sided findings). So are `bytes`, a date or a timestamp against a number or
  against each other. A `text` or `char` key pairs with any class: its texts are what the column holds.
  A key column either side maps with a key map is not judged: its key text is the map's (TABLES §10).
- **Refused as key (P6):** every other class, a timestamp with fractional seconds, `FLOAT(n)`, a
  `ZEROFILL` decimal, `NCHAR`, `NVARCHAR2`, a LOB, and an Oracle column whose declared collation is
  neither `USING_NLS_COMP` nor `BINARY`. Against a service side or passed keys TABLES §5's limit stands:
  key texts that differ never pair, so cast in a view.

**Order.** PostgreSQL `convert_to(k, 'UTF8')`, unchanged. MySQL `CAST(<key text> AS BINARY)`: observed
order `B Z a "a " e é 😀` where the column's default collation gives `a "a " é B 😀 e Z`. Oracle in an
AL32UTF8 database: the key text under `NLS_SORT = BINARY` (observed). Oracle in any other character set:
a text key orders by `UTL_I18N.STRING_TO_RAW(<key text>, 'AL32UTF8')`, its UTF-8 bytes, as PostgreSQL's
`convert_to`. Observed on the image's AL16UTF16 `NVARCHAR2`, whose own binary order puts `😀` before
`～` (U+FF5E) while the raw order is the code points'; a single-byte database is not observed, and the
guard below turns a wrong order into a failed run, never a wrong answer. The function returns at most
2000 bytes (ORA-06502 beyond, observed), so there a text key column declared over 500 bytes or
characters is refused, as is an account that may not execute `UTL_I18N` (P5). There
every key text is ordered and compared through the function, number, date and timestamp texts included
(they are ASCII, so the result is theirs), and a per-key read compares the same expression with the
key's bytes; so the refusal meets every run of such a key column, one with passed keys too, and a view
that shortens the key is the way.

**Guard** (`_Stream.head`, every engine): a key below its predecessor, or a first key not after the resume
bound, raises `KeysOutOfOrder` and fails the run (F5), as `census._count` and `sample._distinct` already
refuse: a mis-sort cannot mis-pair silently.

**Lookups.** An index-friendly `t.k IN (<typed values>)`, plus equality of the key text's order expression
with the key part; the connector keeps a row only when the key texts it fetched equal the key. On MySQL a
number is written as the key part spells it (`1.50`, not `1.5`): the optimiser puts an `IN` list's single
constant in the column's place, also inside the key text's comparison, where `CAST(1.5 AS CHAR)` is not
the text of `1.50` (observed: `k IN (1.5) AND CAST(CAST(k AS CHAR) AS BINARY) = X'312e3530'` finds nothing
on a `DECIMAL(10,2)` key, `k IN (1.50)` finds the row). For the same reason the comparison in SQL may hold
for a row whose own key text is another, which the connector's check then drops. Observed on
MySQL's default collation: `k = 'e'` also finds `E` and `é`; the exact condition finds one row by the
primary key. On Oracle a value compared with a `char` column is `CAST(:v AS CHAR(2000))` (`NCHAR(1000)`):
a plain bind finds nothing, a cast to the column's own length truncates a longer value (`ABCD` finds
`ABC`), and the wide cast does neither and keeps the index (all observed). A number key part is bound as
text inside `TO_NUMBER(:k)`. A key part that is not a value of the column's class (letters for an integer,
a number's form whose exponent no decimal holds, as `1e999999999999999999999999999999`; `''` on Oracle)
cannot be on that side: 404 without a query, and the other keys of its batch are served.

**A key the column cannot hold** is TABLES §4's on every engine: the batch is halved until the key stands
alone, then `KeyRejectedError`, so the key is INCONCLUSIVE(skipped) and the other keys of its batch are
served. `Engine.unfit` is PostgreSQL's `UNFIT`; MySQL 1267, 1270, 1271, 1292, 1366, 1525 and 3854;
ORA-01722, ORA-01426, ORA-01438, ORA-01830 to 01861, ORA-12899 and the driver's errors raised while
binding; and `UnicodeEncodeError` everywhere (a lone surrogate: on Oracle it is the one error the driver
raises while binding text). Observed: an emoji key against a `latin1` key column raises 1267;
`2024-02-31 00:00:00` against a `DATETIME` key raises 1525 although it has a date's form, and ORA-01839
against a `DATE` key; 200 digits against a `NUMBER` key raise ORA-01426.

**Resume bound.** MySQL compares rows of order expressions. Oracle has no row comparison: `(o1 > :a0) OR
(o1 = :a0 AND o2 > :a1)`, and an empty bound part writes `o IS NOT NULL` for `o > ''` and `1 = 0` for
`o = ''`, because `> ''` is never true there (observed). A bound compares bytes with order expressions,
so it holds after a key the column cannot hold.

**NULL and empty keys.** Oracle's `''` is NULL, so such a row is counted by `null_keys`: the same `rows_x`
and `skipped_x`. **Case:** `X1` and `x1` stay two keys everywhere. **Key maps:** `identity` and
`prefix_pad` on every engine. Its digits test is PostgreSQL's `~ '^[0-9]+$'`, written without `$`: MySQL
`k <> '' AND k NOT REGEXP '[^0-9]'` (its `'123\n' REGEXP '^[0-9]+$'` is true, observed), Oracle
`LTRIM(k, '0123456789') IS NULL AND k IS NOT NULL` (observed).

## 9. Values: canonical rendering 1 (`render.py`)

The engine renders, the driver carries `str` and `bytes` only, the worker canonicalises. A rendered text
that is not of its class's form is `unconvertible`, never passed on.

| Class | Canonical JSON value | MySQL | Oracle |
|---|---|---|---|
| `integer`, `decimal` | exact number; digests use `jsonio.number_text`, so `1.50`, `1.5` and a `ZEROFILL` column's `00001.50` hash alike | `CAST(c AS CHAR)`; `BIT` through `CAST(c AS UNSIGNED)` | TM9 (`.5`, `1E+125` observed), parsed as `Decimal` |
| `float` | the shortest text that reads back, PostgreSQL's included in such a pair; `"NaN"`, `"Infinity"`, `"-Infinity"` | `CAST(CAST(c AS JSON) AS CHAR)` (shortest, a whole number as `3.0`; observed) | `TO_CHAR(c)`: 17 digits, `Nan`, `Inf`, `-Inf` (observed; TM9 prints `#`), reparsed |
| `float32` | the same for 32 bits | the same expression, which widens the value to a double exactly, narrowed (a plain cast to `CHAR` prints 6 digits: 1.1234567 and 1.1234599 both read `1.12346`, observed) | `TO_CHAR(c)`: 9 digits, narrowed |
| `text` | the string | `CONVERT(c USING utf8mb4)` | as is; a LOB whole, inside §6's bound |
| `char` | the string without trailing spaces, PostgreSQL's included in such a pair | `CONVERT(c USING utf8mb4)`, as `text` | as is |
| `boolean` | `true`, `false` | none | `TO_CHAR(c)` (`TRUE`, observed) |
| `date` | `YYYY-MM-DD` | `CAST(c AS CHAR)` | none |
| `timestamp` | `YYYY-MM-DDTHH:MM:SS[.f]`, trailing zeros dropped | `DATE_FORMAT`, 6 digits | `TO_CHAR` with `SYYYY`, up to `FF9` |
| `instant` | the same in UTC, then `+00:00`; the stored offset is not compared | the same (session zone UTC) | through `SYS_EXTRACT_UTC` (observed) |
| `time` | `HH:MM:SS[.f]`, trailing zeros dropped, PostgreSQL's included in such a pair | `CAST(c AS CHAR)` (`03:04:05.120` where PostgreSQL prints `.12`, observed) | none |
| `other` | the engine's text; never crosses engines (P8) | none | `TO_CHAR(c)`; a `ROWID` or `UROWID` through `ROWIDTOCHAR(c)` (`TO_CHAR` refuses them, ORA-00932 observed) |
| `bytes` | `\x` and lower-case hex | the bytes (`BINARY(n)` zero-padded as stored) | the bytes |
| `json` | the JSON value, parsed exactly | `CAST(c AS CHAR)` | `JSON_SERIALIZE(c RETURNING CLOB)` (`1.0` reads `1`, observed) |

- **One text in every statement.** MySQL declares the cast of a `DOUBLE` to `CHAR` 22 characters wide,
  and a double's shortest text takes up to 24. Observed with `CAST(c AS CHAR)`: a single `SELECT` of a
  base column returns the text whole, but the `UNION ALL` of a per-key batch and a scan that sorts in a
  temporary table cut it to 22 characters (`-1.1102230246251565e-16` comes as `-1.1102230246251565e-1`,
  still a number's form), and a view's expression (`d * 1`) is rounded to fit, to 16 digits and to 14
  with a three-digit exponent, in every statement: one key gave another verdict by the size of its batch,
  a value read as its tenth, and two different doubles of a view hashed alike. Through `JSON` the text
  has no declared width: observed whole for a base column and a view's expression, alone, in `UNION ALL`
  arms and in a scan with a temporary table, at both extremes of `DOUBLE` and for `FLOAT`. Every other
  expression of §8 and of this table was observed at its type's longest text in the same statements on
  both engines (`DECIMAL(65,30)`, `BIGINT UNSIGNED`, `BIT(64)`, `TIME(6)`, `DATETIME(6)`, a `prefix_pad`
  key; Oracle's `TO_CHAR` formats and masks): none other is cut or rounded. The corpus holds such rows
  and a test reads each alone, in a batch and through such a scan (§16).
- **Floats are narrowed and shortened in one place.** A 32-bit float's canonical value is found from the
  decimal text by exact arithmetic: rounded to a double first and then to 32 bits, `7.038531e-26` reads
  as the float above the one it is the text of, and two different floats would share one value.
  PostgreSQL prints the shortest text that reads back *alone*, which for some whole floats from 2^25
  (`real`) and 2^54 (`double precision`) is a digit longer than the shortest (`4.6056592e+07`,
  `5.2990648348713776e+16`, observed; 46056590 and 52990648348713780 read back as the same floats): in
  a pair across engines its floats go through the same canonicaliser as the other engines' texts.
- **Years before 1.** Oracle's `YYYY` prints 44 BC and AD 44 alike (`0044-03-15`, observed), which would
  digest two different dates equal. `SYYYY` prints `-0044` and a leading space otherwise (observed); the
  canonical value takes the form PostgreSQL's `to_jsonb` gives a BC value, pinned in the corpus.
- **MySQL zero dates.** A zero or partly zero `DATE` or `DATETIME` (`0000-00-00`,
  `2024-00-00T00:00:00.000000`, observed) is of its class's form: it keeps its text, zeros dropped as for
  any timestamp, equals only the same text on another MySQL side, and is a reported difference against
  any real value.
- **MySQL `TIME`** outside a day (`-12:00:00.5`, `838:59:59`) keeps its text under the same rule and
  equals no time of day.

**Reported as differences until the approved spec bridges them:**

| Situation | Declared bridge |
|---|---|
| Oracle NULL against `''` | `typeMap {col: {as: string, emptyAsNull: true}}` on the other side |
| Oracle `DATE` against a date (P9) | `{as: date}` on the Oracle side (drops the time), or `{as: timestamp}` on both |
| `TINYINT(1)` or `NUMBER(1)` against boolean (P9) | `{as: boolean}` on the number side |
| unzoned against zoned timestamp (P9) | `{as: timestamp, naiveTimezone: ...}` on both |
| nanoseconds against microseconds | `{as: timestamp}` on both (6 digits kept) |
| zero dates, `BINARY` padding, `uuid` against `RAW(16)` | a view |

**`emptyAsNull`** is a new boolean `typeSpec` member (both schema copies), valid with `as: string` only
(`invalid_type_map` otherwise): `convert` returns null for an empty string. It acts in digests and per-key
rows alike, so such rows match in bulk; a normaliser would send each through a per-key read.

## 10. Statements (`compose.py`)

**Injection-free by construction.** A statement is fixed text, identifiers and values. Identifiers come
only from the catalogue's spelling (MySQL backticks, doubled; Oracle double quotes). Oracle values are bind
variables typed by class. No permissively licensed MySQL driver binds on the server, so a MySQL value is
exactly one of: a number matching `-?[0-9]+(\.[0-9]+)?` (as written, else written out without an
exponent); `_utf8mb4 X'<hex of its UTF-8>'`; `X'<hex>'`.
Nothing is escaped and the driver's `%` interpolation is unused, so the refusal of `%` in names stays
PostgreSQL's.

**Per-key read**, one arm a key, as TABLES §4:

```sql
(SELECT 0, <rendered columns>, <oversize>, <key texts>, CASE WHEN (<where>) THEN 1 ELSE 0 END
 FROM `ledger`.`customers` AS t WHERE t.`id` IN (7) AND CAST(CAST(t.`id` AS CHAR) AS BINARY) = X'37'
 ORDER BY <its position> DESC LIMIT 2) UNION ALL (...)
```

Each arm puts the row its filter selects first, ordering by that flag's select position (`ORDER BY 5
DESC`): any alias may be a column's name, and Oracle refuses an arm ordered by an alias `s` that also
selects a column named `S` (ORA-00960, observed), which the probe, having no such order, did not meet.

The `relation` primitive writes the table and its alias: `AS t` on MySQL, `"APP"."CUSTOMERS" t` on
Oracle, which refuses `AS` there (ORA-03048, observed). Oracle writes each arm as MySQL does, `(... ORDER
BY 5 DESC FETCH FIRST 2 ROWS ONLY)`: wrapped as `SELECT * FROM (...)` an arm that selects one expression
twice (a key column that is also compared) is refused (ORA-00918, observed). It reads a group of 200 in
statements of at most 50 keys, arm counts padded to 1, 2, 4, 8, 16, 32 or 50 by repeating the last key's
arm, its binds included, under position -1, discarded: seven statement texts a plan, each statement one
rate-budget token. The worker builds the document, applies `typeMap` and records `jsonio.dumps` of it as
the body.

**Scan.** `SELECT <key texts>, [<row hash>,] <rendered worker columns>, <oversize> FROM t WHERE <key texts
NOT NULL> [AND (<where>)] [AND <after>] ORDER BY <orders>`. Each fetched batch is canonicalised and
hashed off the event loop (a PostgreSQL pair's on it: §5, §15).

- **Across engines** every compared column is a worker column:
  `digest = sha256(EMPTY + jsonio.dumps(convert_row(document, types), sort_keys=True))`, with `EMPTY =
  sha256(b"{}")`. That is what `_Stream._digest` computes today for a PostgreSQL side whose columns are
  all digest columns (its SQL hash of the empty lateral row is `EMPTY`), so PostgreSQL's `scan_sql` is
  unchanged in a cross-engine pair; only the `char`, `time` and float rules of §9 are applied to its
  document.
- **In one engine** (both sides MySQL, or both Oracle with equal database and national character sets) a
  compared column is an engine column when both sides declare the same type for it, neither side's
  `typeMap` names it and it is no LOB column; the others stay worker columns. The engine hashes the
  engine columns' rendered texts and `digest = sha256(row hash + the worker columns' JSON as above)`, the
  shape `_digest` has today; without worker columns it is the row hash, and the engine sorts keys and 32
  bytes.
  - MySQL: `UNHEX(SHA2(JSON_ARRAY(<texts>), 256))`, a `bytes` column as `HEX(c)`, every other as §9
    renders it (a float's whole text, so two doubles of a view hash apart). NULLs and boundaries
    stay distinct (`('ab','c')` against `('a','bc')`, `(NULL,'a')` against `('a',NULL)`, `('',NULL)`
    against `(NULL,'')`, observed). A NULL hash digests as `resource_limit:<side>`. The hash is the
    SHA-256 of the array's text, `["ab", "c"]` (observed), in utf8mb4 whatever a column's or the server's
    character set, so two MySQL sides need no equal character sets.
  - Oracle: `STANDARD_HASH(STANDARD_HASH(x1, 'SHA256') || STANDARD_HASH(x2, 'SHA256') ..., 'SHA256')`: 32
    bytes a column whatever it holds (a NULL hashes as the empty string, which Oracle does not tell from
    it; observed), at most 60 columns a block, the blocks hashed again. It refuses a `CLOB` (ORA-00902,
    observed), hence the LOB rule. `||` joins two hashes as their 64 upper-case hexadecimal digits each
    (observed), so 60 are 3840 of a text's 4000 bytes and 63 are refused (ORA-01489, observed). A text is
    hashed in its database's bytes, an `NVARCHAR2` in the national character set's (observed: `N'ab'`
    hashes as its UTF-16), hence the two equal character sets.
  - "The same type" is the plan's `declared` text, equal on both sides: `varchar(100)`
    in two character sets is one type (its text is hashed as utf8mb4), as are two `enum` columns whatever
    their members (the member's text is hashed); `VARCHAR2(100)` and `VARCHAR2(100 CHAR)` are two. The engine columns are hashed in the order of their canonical names, which both sides
    share. The probe (§7) parses the hash too, so a hash a server refuses is refused when the run is
    created (P13).
- A row that does not convert digests as `unconvertible:<side>`, as today.

**Soundness** is TABLES §5's: equal digests mean identical converted values of the same canonical columns,
and per-key rows are built by the same `document` and `types`. For an engine column, a canonical value is
a function of its class and rendered text alone, and both sides share the class and the render
expression, so equal hashes mean equal values; two texts of one value (a float printed differently by two
server versions) only make a candidate, which its per-key read settles. As for a PostgreSQL pair's plain
columns, two equal engine columns match whatever they hold: a text the worker could not convert is found
only where the worker hashes its column, or reads its row per key.

**Cost.** Across engines every compared column of every row crosses the network to the worker, and each
engine sorts the population with its compared columns: temporary space grows with row width, not
PostgreSQL's 63 bytes a row (not measured on MySQL or Oracle). The digest alone costs 6 to 9 µs a row a
side for 4 columns and 11 to 39 µs for 24 (two micro-measurements on one laptop, not benchmarks).
**Measured** (§15; each run once): 10M rows a side of 4 columns between PostgreSQL and MySQL
scanned in 280 s, 3.6 times the 77.5 s of a PostgreSQL pair's scan in BENCHMARK, the worker's process at
about one core throughout, and the API's `GET /v1/runs/{id}` at 162 ms at the 95th percentile under `ds
serve` against 10 ms with `ds serve --no-worker` beside `ds worker`, which DEPLOY tells a site with
cross-engine scans to run. A same-engine pair is near PostgreSQL's cost: two MySQL tables of 10M rows
scanned in 64 s, and 1M rows a side took 6.4 s (MySQL) and 6.2 s (Oracle) against PostgreSQL's 6.8 s.

**Census, null keys, probe:** the same builders. **Frame:** key texts, then the strata columns' rendered
texts, ties broken by them in binary order; the worker builds the strata JSON; a LOB, JSON or `other`
strata column is refused (P10). SAMPLING's selection digests are Python's and do not change.

**Filters** follow each database's own comparison: MySQL the column's collation (`status = 'active'` also
selects `ACTIVE` on a case-insensitive column), Oracle binary. On Oracle a literal compared with a `char`
column (`=`, `<>`, `<`, `>`, `IN`, `BETWEEN`) is §8's `CAST(:w AS CHAR(2000))`: a plain bind selects no
row at all, silently (observed: `st = :w` finds 0 of 1), and the wide cast selects what the same literal
text does (observed for `=`, `<`, `IN` and `BETWEEN`). A literal over 2000 bytes there is refused (P11;
over 1000 characters against an `NCHAR`, cast to `NCHAR(1000)`). `LIKE` binds its pattern plainly, keeps
Oracle's own semantics and gets `ESCAPE '\'`. `TRUE`, `FALSE` and a bare column need a `boolean` class
(P11). An empty literal is bound as it is: NULL to Oracle, so its comparison selects no row, as the same
filter does there in any client. A number is bound as text inside `TO_NUMBER`, a timestamp inside
`TO_TIMESTAMP` (`TO_DATE` against a `DATE`, so its index serves), bytes inside `HEXTORAW`.

## 11. Features and failures at run time

**In on both engines:** scans, soak re-diff, explicit keys, `key.source`, `compare_one`, mixed pairs,
batched reads, key census, frames, composite keys, key tables. Captured samples stay service-only. **Census records**
of a MySQL or Oracle side add `engine` and `consistency`, and every table side of a pair with such a side
adds `server`, the version its read met (§13); a PostgreSQL pair's are unchanged.

| Class (codes) | Per key | Scan | Census; frame |
|---|---|---|---|
| unfit (§8) | the batch halved; alone, `KeyRejectedError` | as any other server code | the same |
| unreachable (no server code, §5; MySQL 1040, 1053, 1129, 1203, 1226, 1317, 4031; ORA-00018, 00020, 00028, 01033, 01034, 01035, 01089, 02391, 02396, 03113, 03114, 03135, 03136, 12170, 12514, 12516, 12518, 12520, 12537, 12541, 12547, 12571) | `connect_error`, re-read once on a new session | paused (F1), resumed after `worker.lease_seconds`, without limit, as PostgreSQL's | `{failed}`; postponed |
| timeout (3024; DPY-4024, ORA-01013) | `timeout`, then each key alone | none set | `{failed}`; the existing timeout message, naming the code |
| snapshot lost (§6) | `connect_error` | resumed; stalled when the pass saved no checkpoint (F2) | `{failed}`; F7 |
| login refused (1045, 1862, 3118; ORA-01017, 28000, 28001) | `auth_error`: the run pauses (`AUTH_PAUSE`) | the run pauses (`AUTH_PAUSE`) | `{failed}`; F7 |
| temp space (1, 3, 1021, 1028, 1114; ORA-01652) | fails the run | fails the run (F3) | `{failed}`; fails the run |
| denied (1054, 1142, 1143, 1146; ORA-00942, 00904, 01031, 41900) | the schema guard looks first (PRECHECKS §7, V5): a breaking change pauses, else the run fails | the same (F4) | `{failed}`; the schema guard looks first (V5): a breaking change pauses, else the run fails |
| any other server code | `connect_error` naming the code; the session is dropped | resumed; stalled when the pass saved no checkpoint (F4) | `{failed}`; F7 |
| unsafe target (`vetted`) | `connect_error`, detail `UnsafeTargetError` | fails the run (F8) | `{failed}`; F7 |

- **Stalled passes.** A pass that saved a checkpoint resumes (`snapshots_x` + 1, the existing warning).
  One that ends on a server's code having saved none is stalled, and the third stalled pass in a row,
  counted by the worker, fails the run. So an outage is waited out without limit, and a code every resume
  would meet again cannot loop for ever, which a PostgreSQL scan's `temp_file_limit` still can (TABLES §5).
  The worker counts in memory, comparing the scan's checkpoint number when the pass started with the
  stored one when it ended: a restarted worker counts anew. A pass that saved a checkpoint resumes at
  the next sweep; a stalled one is followed by `worker.lease_seconds`, as an unreachable target is, so
  three stalled passes span two lease periods (4 minutes by default), not three sweeps.
- **Codes observed later.** An account at its own `max_user_connections`, which DEPLOY tells
  the DBA to set, answers a connect with 1226, not the 1203 of the server's limit (observed on 8.4.11);
  an Oracle database in restricted session answers ORA-01035 (observed on 23.26.3): both are waited out.
  MySQL 8.4.11 answers a sort that fills its temporary directory with 3 (`Error writing file`, observed
  with a 16 MiB `tmpfs`), none of 1021, 1028 and 1114: 3 and its sibling 1 (`Can't create/write to
  file`, not observed) are temp space. 1030 can carry the same cause and is not classed: only the code
  is kept, and it names others too.
- A stale password is never retried in a loop: an Oracle profile would lock the account.
- **A key table's read** ([CONTRACTS.md](CONTRACTS.md) §11) plans and reads as a key census does, in one
  snapshot. A failure refuses the run's creation, 422, by its code alone: `the key source timed out
  after 3600 s (<code>)`, `... could not be held in one snapshot (<code>)`, `... could not be read: the
  target refused the credential (<code>)`, any other `... could not be read (<code>)`; a plan's refusal
  reads as a side's, `the key source` in place of `side a`. At a rolling soak's cycle end it keeps the
  previous keys (`rolling_resolve_failed`), but a refused login pauses the run (`AUTH_PAUSE`), as a
  scan's does. A PostgreSQL login refusal reads as an outage, as on a table side.
- **A fixed side's stalled pass** ([FILES.md](FILES.md) §10). A statement that fails on files a stat check
  found unchanged (a damaged page or compressed stream) meets the same bytes at every resume: the session
  marks the error (`TargetRefused.fixed`) and the worker counts such a pass as stalled whether or not it
  saved a checkpoint, so the third in a row fails the run. A file side's census record holds no
  `snapshots` and it gets no several-snapshots warning: a resumed pass reads the same pinned bytes. The
  scan's state counts its passes as `snapshots_x` like any side's, which `GET /v1/runs/{id}` answers
  (FILES §11).
- **A file reader's end** ([READER.md](READER.md) §9). The files engine reads through a child process.
  Where that process ends under a statement (a fault of DuckDB's, the kernel's memory killer, a kill for
  a deadline) the statement is `Busy('Died')`, an outage as any `TargetUnreachable`: a scan pauses and
  resumes, a per-key group is read once more, on a new reader. `Busy('Alone')` (another export is read
  apart first) and `Busy('Unstarted')` (the reader's process did not start) are outages too. The third
  end charged to one side's files alone is `ReaderDied`, which fails the run (`ReaderDied: side a: the
  file reader's process ended 3 times (SIGBUS) while only this side's files were being read: they are
  read no more until DeltaSleuth restarts`; `error_type` `ReaderDied`).

## 12. Messages (binding; each asserted)

**Targets file** (`TargetsError`: `ds serve` and every `ds` command exit 2):
- T1 `invalid_database_target` `engine mysql needs database and takes no service` (and `engine oracle needs
  service and takes no database`)
- T2 `allow_unencrypted: true takes no tls_ca_ref, tls_server_dn or tls_verify_name`
- T3 `tls_server_dn is Oracle's`
- T4 `private_network` `loopback and private hosts need allow_private_network: true`
- T5 `forbidden_host` `169.254.169.254 is never a target (link-local, unspecified, multicast or metadata)`
- T6 `engine oracle with TLS needs tls_server_dn (the server certificate's subject), or tls_verify_name:
  false`
- T7 `tls_verify_name: false needs tls_ca_ref and takes no tls_server_dn`
- T8 `tls_server_dn is not a certificate's subject as RFC 4514 writes one (CN=crm-db.bank.example,O=Bank,
  no space after a comma)`

T1 to T3 and T6 to T8 carry the code `invalid_database_target` at the target; T4 and T5 at its `host`.

**The guard at a connect** (`UnsafeTargetError`, from `netguard.vetted`; never the notification wording):
- G1 `10.1.2.3 is a private address: set allow_private_network on the target`
- G2 `169.254.169.254 is never a target (link-local, unspecified, multicast or metadata)`

**Spec load** (422 on upload; `ds spec validate --targets` exit 2; in `rules._targets`):
- L1 `target_kind` `this side needs a database target (dsn_ref or engine)`
- L2 `not supported: graph parity with a MySQL or Oracle side; export the link facts into PostgreSQL`
- L3 `not supported: a regex key map on a MySQL or Oracle side; compare a view that derives the key`
- L4 `not supported: ILIKE on a MySQL or Oracle side; use LIKE`

**Run creation** (`ValueError`: 422 from `POST /v1/runs` and `POST /v1/compare`; `ds run` and `ds compare`
exit 2). None carries a value or a driver's text.
- P1 `side a: cannot connect to target crm-legacy (oracle crm-db.bank.example:1521/CRMPDB as ds_reader):
  DPY-6001: the listener does not know the service` (the thin driver's code for the listener's 12514,
  which reaches it without a server number; ORA-12514 has the same gloss). Other glosses: ORA-01017, 1045
  `the credential was refused`; ORA-28000, 3118 `the account is locked`; ORA-28001, 1862 `the password
  has expired`; ORA-12541, a refused or timed-out socket, DPY-6005 `no server answered`; `gaierror` `the
  host name does not resolve`; DPY-3001 `the server requires Native Network Encryption: ask for a TCPS
  listener`; DPY-3015 `the account has only a 10G password verifier`; 2026 from a server without TLS, or a
  session that reports none (an Oracle session's own protocol stands in the code's place: `tcp`), `TLS:
  the server offers none`; a certificate failure, a subject that is not `tls_server_dn` among them, `TLS:
  the server's certificate was not accepted`; another handshake failure `TLS: the handshake failed`,
  which is also said of a connection reset, aborted or broken before the logon of an Oracle target that
  declares TLS (a plain listener resets a handshake's first bytes, observed: `ConnectionResetError: TLS:
  the handshake failed`); an exception PyMySQL raises that is not its own `the answer is not a MySQL
  server's` (`ValueError: the answer is not a MySQL server's`); G1 or G2; any other code alone.
  A gloss follows its code (`MySQL 1045: the credential was refused`); a failure without a server's code
  shows its class in the code's place (`gaierror: the host name does not resolve`,
  `SSLCertVerificationError: TLS: the server's certificate was not accepted`, `ConnectionRefusedError: no
  server answered`). A PostgreSQL target keeps `cannot connect to target <id>: <libpq's first line>`.
- P2 `side a: target ledger is MariaDB 11.4: MySQL 8.0.17 or later is supported, MariaDB is not`
- P3 `side a: target crm-legacy is Oracle 12.2: 19c or later is supported`
- P4 `side a: the session could not be set (NLS_SORT is BINARY_CI): a logon trigger overrides it`, naming
  whichever of the five settings read back differently
- P5 `side a: key column 'ACCT' is VARCHAR2(1000) in a WE8MSWIN1252 database: a scan orders text keys of
  up to 500 bytes there (compare a view that shortens it)` (every run of that key column is refused, §8);
  `side a: the role may not execute UTL_I18N, which orders text keys in a WE8MSWIN1252 database: grant it
  EXECUTE`
- P6 `side a: key column 'RATE' (BINARY_DOUBLE) cannot be a key: compare a view that casts it`; `key column
  'amount' is NUMBER(10,2) on side a and numeric(12,4) on side b: a number key needs one scale on both
  sides (compare a view that casts it)`; `key column 'day' is date on side a and DATE on side b: the key
  text of a date is never that of a timestamp (compare a view that casts it)` (likewise `a number`,
  `bytes`; never for a key column with a key map)
- P7 `side a: column 'GEOM' (SDO_GEOMETRY) cannot be compared: compare a view that casts it`
- P8 `side a: column 'span' (INTERVAL DAY(2) TO SECOND(6)) cannot be compared with another engine: compare a
  view that casts it`
- P9 `side a: column 'opened' is a timestamp (DATE) and side b's a date: declare typeMap {OPENED: {as:
  date}} on side a, or {as: timestamp} on both`, and likewise for each P9 row of §9 (`{FLAG: {as:
  boolean}} on side a`; `{as: timestamp, naiveTimezone: ...} on both`); two families §9 does not bridge
  end `: declare a typeMap that gives both sides one type, or compare a view that casts it`. A family
  reads `a number`, `text`, `a date`, `a timestamp`, `a zoned timestamp`, `a time`, `a boolean`, `bytes`,
  `JSON`, or `a mapped timestamp` for a column its typeMap declares one
- P10 `side a: strata column 'doc' (CLOB) cannot stratify a frame`
- P11 `side a: the filter compares column 'amount' (NUMBER(10,2)) with a literal that is not a number`
  (a number whose exponent no decimal holds is none; likewise `a date`, `a timestamp`, `a time`, `bytes (\x and hexadecimal digits)`, and `TRUE or FALSE`
  for an Oracle `BOOLEAN`);
  `side a: the filter tests column 'flag' (tinyint(1)) as a boolean: compare it with 1 or 0`; `side a:
  the filter compares column 'ST' (CHAR(3)) with a literal over 2000 bytes`
- P12 `side a: table or view APP.CUSTOMERS not found or not visible` (a database link adds `: a synonym over
  a database link is not read`); `side a: the role may not read APP.T: grant it READ` (`SELECT` on MySQL);
  `side a: column 'secret' is not in ledger.t, or the role may not read it: grant it SELECT`
- P13 `side a: the database refuses the filter or key map: ORA-01722`
- P14 `side a: table or view APP.BROKEN_V cannot be described: ORA-04063` (the relation as the spec
  spells it; whatever a database answers the catalogue statements, or the zero-row statement that
  proves a grant, with after the connect: a view left invalid by DDL on its table, observed on 23.26.3,
  or a session the server ended, by its code, with its gloss where the engine has one; a PostgreSQL
  error by its class, `QueryCanceled`);
  `target crm-legacy: the session's account of its server could not be read: ORA-03113` (`ds target
  check` alone, from `Engine.server`; from a server that is no MySQL server, `RuntimeError: the answer
  is not a MySQL server's`).

**Run time** (the run's `error`, the Run screen; `ds run` exits 2):
- F1 log `scan of run R-... paused: ORA-03113`
- F2 `SnapshotLost: ORA-01555`
- F3 `TargetTempSpace: ORA-01652`
- F4 `TargetDenied: ORA-00942`; `TargetRefused: <code>` (the third stalled pass). A read refused
  with a code of the denied class (a dropped column, 1054 or ORA-00904, among them) first has the schema
  guard look at the run's tables ([PRECHECKS.md](PRECHECKS.md) §7, V5): a change that breaks the plan
  pauses the run (`run_paused_schema_drift`, its lines in place of F4), and otherwise the run fails with F4
- F5 `KeysOutOfOrder`
- F6 `EngineChanged: side a: target ledger is mysql now; this run was planned for oracle`
- F7 (`DrawError`: the run pauses) `the frame of side a could not be held in one snapshot (ORA-01555)`;
  `the frame of side a could not be read: the target refused the credential (ORA-01017)`; `the frame of
  side a could not be read (<code>)`
- F8 `UnsafeTargetError: 10.1.2.3 is a private address: set allow_private_network on the target`

## 13. Interfaces

- **API.** No new route. `GET /v1/targets` (viewer) adds `engine` per item (null for a service), never host
  or user. `GET /v1/runs/{id}` carries the plan's new members in `params.tables`.
- **CLI.** `ds serve --no-worker`. `ds target check ID [--table [SCHEMA.]NAME] [--config FILE]` (new
  `target_app`; opens no store, reads no row, not audited): the firewall, TLS, session and grants
  pre-flight, PostgreSQL targets too. It alone prints the full version, and a note when the server is not
  an observed one or its character set limits keys. Exit 0, or 2 with §12's P messages, `target <id>` in
  place of `side a`. What a server says of itself is printed without its control characters (those
  `str.isprintable` refuses: a MySQL greeting's version is the server's to choose, and an escape sequence
  once reached the terminal), the version and the character set cut at 64 characters, a relation's
  name at 300, a column's name and declared type at 150.
  `ds worker` and the worker inside `ds run` stop on SIGTERM as the one inside `ds serve` does (§6):
  `ds worker` then exits 0, `ds run` as on Ctrl-C (`interrupted`, 130).

  ```
  target crm-legacy: oracle 19.21.0.0.0, character set WE8MSWIN1252, TLS yes, session ok
  note: this release is observed on Oracle 23.26 and MySQL 8.4 only
  note: a run takes text keys of up to 500 bytes in this character set
  APP.CUSTOMERS (table, consistency snapshot)
    CUSTOMER_ID  NUMBER(10)          integer    key
    EMAIL        VARCHAR2(120 CHAR)  text       key
    OPENED       DATE                timestamp  key
    GEOM         SDO_GEOMETRY        -          not comparable
  ```

  The second note says `a run takes`, not `can be scanned`: the limit meets
  every run of such a key column (§8), and §17's prerequisite says the same. The first note is printed
  for a MySQL or an Oracle server whose major and minor version is not the observed one (`OBSERVED`), and
  for no PostgreSQL server. A column's last word is `key` when its key text is one every engine renders
  alike (`Column.keyable`: between two PostgreSQL tables any column can be a key), `not comparable` for a
  type no plan reads, `not readable` for a column the account is not granted, and nothing otherwise.
  Outside AL32UTF8 the check also proves `EXECUTE` on `UTL_I18N` (P5's second message). It exits 2 for
  an id the targets file lacks (`target 'x' is not in the registry`), for a service (`target x is not a
  database target (dsn_ref or engine)`) and for a targets file that does not load, SQL Server's
  `engine` among its errors. `ds profile` is unchanged (D11): its draft's side refuses a database
  target, `this side needs a service target, not a database`.
- **UI.** `Run.tsx` `scanLine` appends ` Side A: oracle 19.21 (snapshot).` for each table side whose
  plan names an engine; `api.ts` `TablePlan` gains `engine`, `server`, `consistency`, `tls`, `classes`;
  `packs.ts` mirrors the wording below. No new screen. `CensusSide`'s scan, frame and
  table records gain `engine` and `consistency`; `sideLine` says the unpinned wordings where it says `in 1
  snapshot` (a scan, a frame; its line of a key census names no snapshot) and ends every engine record
  with the redaction line; the new `censusWarnings` gives the warning below, which the Run screen's
  evidence panel and the approval review show under the census line.
- **Certificate** (`census.described`, `census.warning`). Where `in 1 snapshot` stands today, `unverified`
  reads `in 1 transaction, a snapshot not observable (a MySQL view)` and `none` reads `in 1 pass, not a
  snapshot (a MySQL table outside InnoDB)`. Both add the warning `SIDE A NOT READ IN A SNAPSHOT (<that
  reason>): rows written during the read may be missing or read twice`. PACK §1.3's churn sentence holds
  for `snapshot` only. A census with a MySQL or Oracle side adds `rows and values as the account reads
  them (redaction, masking and row policies apply)`. A resumed scan of such a side reads
  `in 3 transactions, ...` or `in 3 passes, ...`, and its existing warning `SIDE A READ IN 3
  TRANSACTIONS` (`PASSES`), never `snapshots` (§2); `census.warning` gives both warnings, one a line; the
  redaction line ends the description of each side whose record names an engine, after `; `.
  **A sampled pack's claim** (`claim_text`, the certificate's "What the signature proves"; SAMPLING
  §11.3) says of its frame `a table in one snapshot, a listing as returned`. Where a record of the
  frame's census names a `consistency` other than `snapshot`, it says `a table in one pass not observed
  to be a snapshot` instead (`pack.PROVES_SAMPLED_UNPINNED`). A frame whose records name `snapshot` or no
  `consistency`, PostgreSQL's among them, keeps its text, and `ds packs verify` reads no claim text.
- **Packs and reports.** `pack._subject` adds `engine` and `server` per side when the plan has them;
  `pack_verify._subject` prints them (it reads with `.get`, so older packs verify); the format stays
  `deltasleuth-pack/1`. **`server` is the version the cycle read.** The plan's is the one its run was
  created on, and a server may be patched or replaced before a scan, between a soak's cycles or before a
  resume. So each scan pass, key census and frame records the major and minor version of the server its
  session met (`Engine.release`): a scan in its state (`server_a`, `server_b`: the last pass's, saved
  with that pass's first checkpoint), a census and a frame in their record (`server`), for every table
  side whose plan names an engine. The subject, and the certificate's subject line, name the version the
  attested cycle's record holds, the plan's when it holds none, and where the two differ the certificate
  warns `SIDE A READ ON oracle 19.22: the run was planned on 19.21`. Another version fails no run and
  changes no plan; `facts/plan.json` keeps the plan's. Per-key reads record none. The coverage CSV gains `engine_a` and `engine_b`; the PDF "Tables" line names them.
  The certificate's subject line of such a side ends `(oracle 19.21)`. In the CSV a
  table side whose plan names no engine reads `postgresql` (as `named` reads it) and a service side is
  empty; the PDF line adds `(mysql)` after a side's relation only when its plan names the engine, so a
  PostgreSQL pair's line is what it was.
- **Audit.** No new action. `create_run`'s `plan_sha256` binds engines, versions and classes; `run.census`
  adds `engine` and `consistency` when the record has them; a refused login pauses as
  `run_paused_auth_error`.
- **Licence.** No fence (O2): `licence.admit` is untouched. **Migration.** None: the plan, scan state and
  census are JSON already.
- **Table pre-checks** ([PRECHECKS.md](PRECHECKS.md) §11) read every engine through this seam: table pre-checks of
  stored specs or of two declared database targets' schemas (eight routes, `ds precheck`, the Pre-checks
  screen) and the schema guard's lines on a run (`GET /v1/runs/{id}`'s `schema`, the Run screen; `ds runs`
  prints a guard pause's first line). `ds target check` is unchanged.

## 14. Security review points

1. New outbound destinations are each engine target's declared host and port. Every connect resolves,
   vets every answer and dials a vetted address itself (MySQL) or hands the driver that address (Oracle):
   no path lets a driver resolve the name again (§4). An IPv6 answer that stands for an IPv4 address
   (mapped, translated, the well-known NAT64 prefix, compatible) is vetted as that address; a
   network-specific NAT64 prefix is not known to the guard. **Not vetted:** the address an Oracle listener
   redirects to (D12). Service targets refuse redirects; the thin driver cannot. DEPLOY and PENTEST say
   so, and tell a site to bound an Oracle target with an egress rule listing its node addresses.
2. TLS cannot be downgraded: a MySQL server offering none is refused before authentication (§3, §4), and
   Oracle is TCPS or, declared, plain. Chain-only verification (`tls_verify_name: false`) needs a declared
   CA and accepts any certificate that CA issued. An Oracle certificate's subject is matched whole and
   exactly by DeltaSleuth, in the handshake, not by the driver, whose match another certificate of the
   same CA could pass (§3, §4).
3. Oracle encryption is TLS (TCPS) only: thin mode has no Native Network Encryption, Kerberos or RADIUS.
4. MySQL under `allow_unencrypted` trusts the RSA key the server sends for `caching_sha2_password`.
5. MySQL key values, filter literals and resume bounds travel as hex in statement text; the site's general
   log, slow log and `performance_schema` may hold them. DeltaSleuth logs no statement or driver message.
6. A document-heavy target: no LOB total over 16 MiB crosses the wire from MySQL or Oracle; a scan's
   fetch is bounded in bytes on MySQL and by declared width on Oracle, and sized by the fetch before it
   on the PostgreSQL side of a cross-engine pair; a per-key group of rows that may hold LOBs is 4 keys
   (§6); the 64 MiB limit of a per-key row and `jsonio`'s limits stand. A hostile or broken MySQL
   endpoint: `local_infile` off (a request for a file is refused by the driver, and the refusal is a
   coded error); one 10 s budget ends the connect and the handshake however slowly bytes come; a packet
   sent in parts is refused past 80 MiB; whatever the driver raises leaves `mysql.py` as a code or a
   class name, never the server's text (§5); and `ds target check` prints what a server says of itself
   without control characters (§13). **Not bounded:** the rows of a buffered answer (a server that
   answers a per-key or a catalogue statement with rows without end is read until memory ends it) and
   a scan's duration (it has no timeout by design). **Not guarded:** DeltaSleuth's own reading of a
   well-formed answer of another shape than its statement's (§5 keeps its own errors as they are),
   but for the one `ds target check` reads first: a server that passes MySQL's handshake and answers
   `SELECT @@character_set_server` with no row is P14 (`IndexError: the answer is not a MySQL
   server's`, held by the stub server's test). A count answered with no
   row, or a catalogue row of other columns, still ends as Python's own error (`IndexError`,
   `ValueError`), which fails the run or the command and holds nothing the server said (read from the
   code). **Not examined:** what a hostile Oracle listener can
   make the thin driver hold (the malformed greetings tried ended as `DPY-4011`). The worker shares a process with the API unless `--no-worker`
   is used.
7. Compared rows reach worker memory in cross-engine scans; nothing more is persisted.
8. Values are compared as the account reads them: under Oracle Data Redaction, VPD column masking or a
   masking view both sides return the same constant and it reads as a match. DEPLOY's grants require an
   account exempt from redaction on the compared columns, and the certificate says what was read (§13).
9. A plan and a pack carry each side's engine and major and minor version (D10); the patch level, host,
   port and user never leave the targets file and `ds target check`'s terminal. One more place names the
   host, port and user, by §12's own wording: P1, the refusal of a connect, to
   whoever creates the run (the answer of `POST /v1/runs` and `POST /v1/compare`, `ds run`, `ds compare`
   and `ds target check`'s terminal), as libpq's first line names a PostgreSQL target's host and user
   today. No plan, pack, report, census record or stored run holds them (`test_tables_security.py` holds
   it for a stored plan). The chat is not such a place: its `compare_key` tool tells the model `side a:
   cannot connect to target crm-legacy` and nothing more (`Unconnected.brief`), so no host, port,
   service or account reaches the model's host or the stored transcript, a PostgreSQL target's libpq
   line included. One address is stored by design:
   a run the guard fails (F8) keeps the address its target's name resolved to in its `error` and its
   `run.finished` audit row; its notification carries the class alone (§5).
10. PostgreSQL `dsn_ref` targets stay unvetted (the host is inside the secret DSN): an existing gap.

## 15. Performance

`DS_PERF=1`, `tests/e2e/test_engines.py`; TABLES publishes the measured times, not these thresholds.
- `test_a_million_rows_across_engines`: 1M rows a side, 4 columns, 11 differing keys; the whole run under
  120 s for PostgreSQL-MySQL, 180 s for PostgreSQL-Oracle and MySQL-Oracle (Oracle Free is capped at 2
  threads). PostgreSQL alone measures 6.8 s.
- `test_a_million_rows_in_one_engine`: the same for MySQL-MySQL under 60 s and Oracle-Oracle under 120 s.
- `test_ten_million_rows`: 10M rows a side, 4 columns, PostgreSQL-MySQL (worker hashing) and MySQL-MySQL
  (engine hashing), under `ds serve`. It records each scan's seconds and its multiple of PostgreSQL's
  77.5 s, the worker's peak resident memory (asserted under 1 GiB) and the p95 latency of `GET
  /v1/runs/{id}` asked once a second during the scan, alone and with `--no-worker`. TABLES, DEPLOY §7 and
  README publish all of them (§3's gate), so a site sizes its cut-over window on measured figures.
- `test_batches_across_engines`: 20,000 differing keys of 100,000 under 120 s (PostgreSQL: 43 s).
- The existing PostgreSQL `DS_PERF` tests must not move. None of them has a typeMap column
  (see "A PostgreSQL pair's digests" below).

`tools/bench.py` is out of scope; BENCHMARK gains one sentence that its results are PostgreSQL's.

**Measured** (2026-10-02; each test once, in a pytest process of its own; TABLES §13.9 has the
conditions: one Mac that was not idle, every database a stock container on it):

| Test | Threshold above | Measured |
|---|---|---|
| `test_a_million_rows_across_engines` PostgreSQL-MySQL | 120 s | 27.9 s |
| the same, PostgreSQL-Oracle | 180 s | 25.9 s |
| the same, MySQL-Oracle | 180 s | 23.0 s |
| `test_a_million_rows_in_one_engine` MySQL-MySQL | 60 s | 6.4 s |
| the same, Oracle-Oracle | 120 s | 6.2 s |
| `test_batches_across_engines` PostgreSQL-MySQL, PostgreSQL-Oracle, MySQL-Oracle | 120 s each | 38.2 s, 35.9 s, 34.1 s |
| `test_ten_million_rows` PostgreSQL-MySQL, the worker in `ds serve` | memory under 1 GiB | scan 280 s (3.6 times 77.5 s), 191 MiB, p95 162 ms (max 246 ms) |
| the same with `--no-worker` and `ds worker` | | scan 278 s, 131 MiB, p95 10 ms (max 21 ms) |
| `test_ten_million_rows` MySQL-MySQL, the worker in `ds serve` | | scan 64 s (0.8 times), 180 MiB, p95 65 ms (max 88 ms) |
| the same with `--no-worker` and `ds worker` | | scan 63 s, 120 MiB, p95 10 ms (max 15 ms) |

As built: `test_batches_across_engines` runs the three cross-engine pairings; `test_ten_million_rows`
seeds one PostgreSQL and two MySQL tables once and runs its four scans in turn, takes a scan's seconds
from the poll that first sees `scan.done` (to the second), the memory from the `ru_maxrss` of the process
the worker ran in, and asserts only correctness and the 1 GiB. The existing PostgreSQL `DS_PERF` tests
were re-run after the seam alone, before a second engine (their figures did not move), and not with these measurements. **Not
measured:** 10M rows with an Oracle side, MySQL 8.0, Oracle 19c, rows wider than 4 columns, a database on
another host, the temporary space of a MySQL or an Oracle sort, a cold cache, and any figure a second
time (PostgreSQL-MySQL at 1M was 27.7 s in a trial run before the table's).

**A PostgreSQL pair's digests.** Handing a PostgreSQL side's fetched batch to a thread for its digest, also
in a PostgreSQL pair with typeMap columns, is a path no `DS_PERF` test runs. Measured (1M
rows a side, 5 columns, a typeMap on a decimal and on a timestamp, 11 differing keys, three alternating
runs each on one scratch `postgres:16-alpine`): 33.4, 33.5 and 33.4 s before the seam, 39.1, 39.2 and
38.5 s with the thread, and 33.2 and 33.4 s with the digest back on the loop; a scan without a
typeMap did not move (7.5 s on both). So a PostgreSQL pair digests on the event loop, and only a
side of a pair with another engine digests in a thread. These figures were not measured
again after the change; a test holds which thread digests (`test_postgres.py`), none holds a time.
The table above was measured before that change, and not again after it: its
rows hold no float and no LOB column, and on the PostgreSQL side of a cross-engine scan a fetch of such
narrow rows is 10,000 after the first 100 (§6).

## 16. Tests

Fixtures (`tests/conftest.py`): `mysql_server` and `oracle_server`, as `pg_server` (`docker run --pull
never`); markers `mysql` and `oracle`; skipped without Docker unless `DS_REQUIRE_DB=1`. Seeds wait out
ORA-01466. A test CA and a server certificate with a subjectAltName are made with the locked
`cryptography` package. TCPS against the Oracle image is not exercised (it has no TCPS listener): the
driver's TLS path is tested against a local Python TLS listener.
`test_integration_contracts.py`'s black-box check does not refuse the word `oracle` (`TARGET_BACKENDS`):
§4's targets, their declaration and T3 and T6 name the engine, and an engine a database target declares is no
hidden backend.

Also: `test_mysql.py` starts a second MySQL server whose temporary directory holds
16 MiB (`--tmpfs /tmp:size=16m`, `--skip-log-bin`) for the code a full one answers a sort with;
`test_tables_security.py` holds a stub MySQL server that greets and answers as no MySQL server does;
the TLS listener of `test_tables_tls.py` presents a certificate of any subject; the terminated worker
is a subprocess of `e2e/test_engines.py`; and memory is asserted with `tracemalloc` (a group's rows, a
fetch, a packet's parts). ORA-01035 was observed by hand (`ALTER SYSTEM ENABLE RESTRICTED SESSION`) and
is held by the code table's test alone: the session's server is shared by every test.
`test_oracle.py` makes a view invalid (its table dropped) for P14, which `test_tables_plan.py` and
`cli/test_cli.py` hold through a fake engine for `describe` and `server`, and `api/test_app.py` as the
422 of both routes; `core/test_notify_events.py`
holds the `error_type` of each of F2 to F8's reasons; `reports/test_pack_contents.py` issues a sampled
pack whose frame record is a MySQL view's and verifies it; `agent/test_tools.py` refuses a connect to
an Oracle target on a closed port and reads what the tool tells the model; `test_postgres.py` holds
which thread digests a PostgreSQL pair's batch and a cross-engine side's.

Beside the table below: the review points of §14 that a test can hold
and none held yet are in `connectors/test_tables_security.py` (no client library or client
configuration; a plan without host, port, user or patch level; nothing a server says logged) and in
`e2e/test_engines.py` (a scan across engines stores nothing of the rows it found identical).
`cli/test_cli.py` drives `ds target check` through a fake engine for its output, notes and exits, and
against each of the three servers; `ds serve --no-worker` and the profiler's refusal are there too. The
acceptance kits run against the MySQL and Oracle containers to their `expected.json`
(`test_the_acceptance_kit_gives_its_expected_result`). `test_integration_contracts.py` holds §17's
prerequisites word for word in DEPLOY and README.

For engine hashing: `mysql_pair.json` and `oracle_pair.json` hold `scan_hashed`, the scan of a pair of one
engine. `test_mysql.py` and `test_oracle.py` pin each engine's hash to a SHA-256 computed in Python, tell
the boundary and NULL cases apart, and show the same rows equal and different in the same places whether
the engine, the worker or both hash their columns, on one table and on two declared differently. Two
Oracle databases that differ in a character set are a fake engine's (`test_tables_plan.py`: the image
has one database), with the plan's other choices of engine and worker columns. In `e2e/test_engines.py`
the MySQL-MySQL and Oracle-Oracle pairs of TABLES §7's dataset run twice, to the same counters and
findings: with the engine hashing the columns both sides declare alike (`email` and `name`; `amount` is
declared at two scales, the worker's), and with a `typeMap` that makes them the worker's too; the soak's
pair is hashed by the engine alone. `EXPLAIN` is `test_a_lookup_uses_the_key_s_index` in both container
files: on MySQL a text's and bytes' hexadecimal digits, an integer's and a decimal's digits and a `latin1`
column; on Oracle a bound text, `TO_NUMBER`, the `CHAR(2000)` cast, `TO_DATE` and `HEXTORAW`.

For Oracle, beside the table below: `oracle_server` starts the image once a session
(about a minute; a test's "database" is a fresh account and its schema), and its `execute` waits after
DDL until a read-only transaction reads the account's tables, which is how seeds wait out ORA-01466. The
image runs with `MAX_STRING_SIZE=STANDARD`, where a column takes no collation (ORA-43929, observed): the
collation rule of §8 is tested on the catalogue's row alone (`test_tables_errors.py`, with the other
rules of an Oracle column). P5 is shown on the image by a session told its database is `WE8MSWIN1252`:
the conversion runs for real, on a `VARCHAR2` and on the `NVARCHAR2` stream. `portable.json` stays the
set PostgreSQL and MySQL share (dates, times, empty strings); the test reads Oracle on the columns it
holds, its `DATE` declared a date, and pins that the row of empty strings differs until `emptyAsNull` is
declared. The Oracle key census and frame are in `runner/test_census.py` and `test_sample_frame.py`,
`oracle_pair.json` holds the golden statements with their binds, and the relay that cuts a scan's flow
serves Oracle too (`DPY-4011`). A session killed during a scan's wait is `ORA-00028`, unreachable.

For MySQL, beside the table below: `mysql_server` starts the image as it is, copies a
certificate of a session-made CA into it and switches the server to it (`ALTER INSTANCE RELOAD TLS`), so
one server presents the SAN certificate and, inside `stock_certificate()`, its stock one. A user-space
proxy that stops forwarding still acknowledges keepalive probes, so a silently dropped flow cannot be made
without a packet filter: `test_guards.py` asserts the keepalive options on the socket, and
`e2e/test_engines.py` cuts a scan's flow through a relay and sees the scan paused and resumed. The resolver
that fails once is there too (it needs a real session), the MySQL frame and key census are in
`runner/test_sample_frame.py` and `test_census.py`, and `mysql_pair.json` holds the golden statements.

| File | Behaviours |
|---|---|
| `spec/test_targets.py` | an engine target loads; excluded members; T1 to T8; a NAT64 form of a private and of the metadata address is T4 and T5; `service`, `host` and `tls_server_dn` take no descriptor characters |
| `connectors/test_guards.py` | `vetted`: literal, resolved private, metadata never, G1 and G2 (never the notification text); the NAT64, IPv4-translated and IPv4-compatible forms of a private address are G1 and of the metadata address G2, and a public address through the translator passes (`test_notify_send.py` holds the same of the notification guard); a second address is tried when the first refuses; a silent first address leaves the next its share of the budget, on MySQL (`_dial`) and on Oracle; MySQL connects to the vetted address with keepalive and `TCP_NODELAY` set; Oracle's `host` is the vetted address in all three TLS modes, the driver matches no name and the context of a declared subject carries DeltaSleuth's TLS object |
| `connectors/test_tables_tls.py` | a stub MySQL server whose greeting omits the TLS capability is refused with the TLS gloss before any authentication packet, also when the password is wrong; the MySQL container with the mounted SAN certificate: full verification passes, a wrong CA and a wrong name are refused, and the stock certificate passes only `tls_verify_name: false` with its CA; the Python TLS listener, for Oracle: a wrong CA and a wrong DN are refused before the logon, and the connection lands on the vetted address; `test_oracle_s_declared_subject_is_matched_whole` (ten certificates of one CA: the declared subject passes, and one that repeats an attribute type with another value, adds an attribute, holds a comma in a value, or differs in order or in case is refused in the handshake); TLS declared against a listener that resets or closes the handshake's first bytes is `ConnectionResetError: TLS: the handshake failed`, and no TLS is said when the target declares none; the installed PyMySQL is 1.2.1 or later |
| `connectors/test_tables_compose.py` | golden statement text per engine (rows, scan with and without a row hash, census, frame, null keys, probe); the `relation` alias; the Oracle bound and its empty part; the `CHAR(2000)` cast for a key and for `=`, `IN`, `BETWEEN` and `<` filters; the decimal key mask; the digits test; arm padding; an arm ordered by its flag's position beside a column named `S`; `test_mysql_statements_hold_only_grammar` (Hypothesis: hostile keys, literals and prefixes); on both engines a literal is a value of the closed grammar or none, a number whose exponent no decimal holds among them (Hypothesis), and such a filter literal is P11 |
| `connectors/test_tables_render.py` | canonicalisers against the corpus; float32 narrowing, by exact arithmetic (the floats `15ae43fd` and `15ae43fe`; Hypothesis: any 32-bit float has one canonical value whatever text renders it); PostgreSQL's floats in a pair across engines, and untouched in a PostgreSQL pair; `Nan`, `Inf`; `char` trim; `time` zeros; BC; zero dates; a text not of its class; `test_digest_equals_postgres_shape`; `test_a_row_renders_alike_in_every_statement` (MySQL and Oracle: each corpus row alone, in a batch and by a scan, from the table and from the joining view, the engine's hash too); `test_the_same_floats_read_and_digest_alike_on_every_engine` (144 floats of each width on the three engines) |
| `connectors/test_tables_plan.py` (a fake `Engine`) | classes per engine; P2 to P14 (an engine's error while it describes a relation, a PostgreSQL error by its class, and `preflight`'s for `server` and `describe`, each a `ValueError` with no cause and no session left open); number key scales, an unconstrained `NUMBER`, `ZEROFILL`; key classes whose texts are never equal; engine and worker columns; families after `typeMap`; `test_postgres_pair_plan_unchanged` |
| `connectors/test_tables_errors.py` | every code of §11 to its class (MySQL 1054 to the denied class); `OSError`, `gaierror`, `SSLError`, `TimeoutError` and an unlisted client code are unreachable; an exception of another class PyMySQL raises is unreachable by its class name; an unlisted server code is `TargetRefused`; no driver text in `str(exc)` |
| `connectors/test_tables_security.py` (a stub MySQL server) | a greeting cut short and one whose version is text or bytes end as P1 or `TargetUnreachable` with the class alone, raised from nothing; a `LOCAL INFILE` request in answer to a statement of the pre-flight, and the character set's statement answered with no row, are P14 with the gloss; a greeting sent a byte at a time ends with the connect budget; a packet of ten 16 MiB parts is refused at its second part (`MySQL 2020`, peak memory asserted) |
| `runner/test_scan.py` | `test_keys_out_of_order_fail_the_run`; `test_first_key_must_follow_the_resume_bound`; `test_snapshot_lost_with_progress_resumes`; `test_three_stalled_passes_fail` (snapshot lost, and another server code); `test_unreachable_passes_never_fail`; `test_temp_space_fails_the_run`; `test_refused_login_pauses_the_scan`; `test_engine_changed_fails`; `test_unsafe_target_fails_the_scan`; a resolver that fails once mid-scan resumes; a stalled pass is followed by a lease period and one that saved a checkpoint is not; `test_a_scan_ends_when_its_run_no_longer_runs` (cancelled, paused); a PostgreSQL pair's scan state names no server |
| `runner/test_census.py`, `web/test/packs.test.ts` | the three wordings, the warning and the redaction line; PostgreSQL records unchanged |
| `canonical/test_convert.py`, `spec/test_tables.py`, `spec/test_schema_copies.py` | `emptyAsNull`; L1 to L4 |
| `reports/test_pack_contents.py`, `cli/test_cli.py`, `api/test_app.py` | subject (major and minor only; the version the cycle read, and the certificate's warning where it is not the plan's), older packs; a sampled pack whose frame record is `unverified` never says `in one snapshot` in its claim, on its certificate or in its manifest, passes `ds packs verify`'s steps, and a `none` record reads the same while `snapshot` and no `consistency` keep the text; `ds target check` output, notes and exits (P14 among them: exit 2, no traceback), and a server's text printed without control characters and cut; `ds serve --no-worker`; `engine` in `/v1/targets`; a database's error while a run is created is a 422 from `POST /v1/runs` and from `POST /v1/compare`, naming its code |
| `core/test_notify_events.py`, `agent/test_tools.py`, `connectors/test_postgres.py` | `error_type` of `<class>: <code>` reasons and of the guard's text is the class, of a secret's message `SecretError`; the chat's `compare_key` tells the model the side and the target id of a refused connect, never host, port, service or account; a PostgreSQL pair's typeMap columns are digested on the event loop and a cross-engine side's in a thread |
| `connectors/test_mysql.py`, `test_oracle.py` (containers) | session settings read back (all five on Oracle; P4); plan: folding, synonym, database link, hidden column, INSERT-only table, refused type, collation; per-key: row, 404, ambiguous, a key not of its class, case and accent twins, a trailing-space key, an Oracle `CHAR` key, a timeout; **unfit keys inside a batch of 200 whose other keys are served:** an emoji and a Cyrillic key against `latin1` and `utf8mb3` key columns, an impossible date against a `DATETIME` key, a 200-digit key against a `NUMBER` key, a lone surrogate; a key of a number's form whose exponent no decimal holds is 404 alone and in such a batch; an Oracle column named `S` is read per key, as a compared column and as the key; a MySQL double is whole in a batch, in a view's expression and in the engine's hash; a session ended by `wait_timeout = 1` between two per-key reads is re-read once, and one killed during a scan's wait (`KILL`; `ALTER SYSTEM KILL SESSION`) resumes; a TCP proxy that stops forwarding mid-scan ends in a paused, resumable scan within two minutes; a row with a 20 MiB LOB is never fetched (`resource_limit`, peak memory asserted) while its neighbours are; a `CHAR(3)` status filter selects the same rows as on PostgreSQL `char(3)` and MySQL `CHAR(3)`; an Oracle view left invalid is refused as P14 by the plan and by the pre-flight; `prefix_pad` skips a key with a trailing newline on all three engines; an `NVARCHAR2`-ordered stream proves the UTF-8 order expression; the row hash tells the boundary and NULL cases apart; `EXPLAIN` shows the key index for hex and digit literals and for the `CHAR` cast; a write refused (1792; ORA-01456); a refused login is `auth_error`; MyISAM `none`, a view `unverified`; a write during an InnoDB scan not seen; ORA-01466 is `SnapshotLost`; an account at its `max_user_connections` is unreachable (1226); a second MySQL server with a 16 MiB temporary directory answers a wide sort with 3, temp space; the PostgreSQL side of a pair with MySQL is fetched by bytes (fetch sizes and peak memory asserted) and a PostgreSQL pair's fetch is what it was; a plan with a LOB column is `large`; an Oracle session's socket is found and holds the keepalive options; TLS declared against Oracle's plain listener gets the TLS gloss |
| `tests/golden/engines/` | `{postgres,mysql,oracle}.sql` seed one column per supported type with edge values, among them `TIME(3)` and `TIME(6)`, a negative `TIME`, 44 BC and AD 44, zero and partly zero dates and datetimes, `ZEROFILL`, each type's longest text on MySQL and Oracle (rows `wide` and `tiny`, doubles of 23 and 24 characters among them), and decimal keys; `corpus_joined`, a view that joins the corpus to its keys (on MySQL its scan sorts in a temporary table) and computes a double; `expected.json`, each engine's canonical document; `portable.json`, the values whose documents and digests must be identical on all three, whole floats at and above 2^25 and 2^54 among them (`test_golden_renderings`, `test_portable_values_digest_alike`) |
| `e2e/test_engines.py` | TABLES §7's dataset in the three engines, plus `CHAR` padding, `''` against NULL with and without `emptyAsNull`, a `DATE` with a time, a zero date, `BIGINT UNSIGNED` maximum, an emoji key, and a `NUMBER(10,2)` key against `DECIMAL(10,2)` and `numeric(10,2)`: counters and findings exact as PostgreSQL-MySQL, PostgreSQL-Oracle, MySQL-Oracle, MySQL-MySQL, Oracle-Oracle (both with engine and with worker columns) and MySQL against a mock service; a PostgreSQL-to-`latin1` MySQL pair whose new side holds an emoji key ends with that key skipped and every other key compared; kill and resume, also past a key the other side cannot hold; resume past an empty part on Oracle; a composite key; `prefix_pad`; the key census; a frame with strata on Oracle; two soak cycles; a key-order corpus on a case-insensitive MySQL column; a run cancelled or paused during the sort ends both statements and sessions within seconds; `ds worker` and `ds run` terminated during a scan (subprocesses, SIGTERM) end both statements and exit 0 and 130; keys of rows that may hold large values are read in groups of 4 (group sizes and peak memory asserted); a cycle records the server version it read, whatever the plan's, and completes |

## 17. Documents this contract amends

- **TABLES:** title and goal; §1 (engine targets); §3 (stored form); §4, §5 (lookups, unfit keys per
  engine, the guard, digests, stalled passes); §7; §9 (literals by class, the `CHAR` cast); §10
  (`emptyAsNull`, key maps per engine); a new §13 "Engines" holding §4 to §11 of this contract and §15's
  measured figures.
- **CONTRACTS:** §1 (`connectors/tables/`), §7 (`/v1/targets`), §11.
- **DEPLOY:** §3 Secrets; §7, opening with the prerequisites below, then declarations, the three TLS modes
  (a certificate with a subjectAltName, or the chain-only mode with the server's CA), grants (with the
  redaction exemption, and `EXECUTE` on `UTL_I18N` outside AL32UTF8), firewall rows for 3306, 1521 and
  the TCPS port with the egress rule of §14 point 1, session counts against `max_user_connections` and
  `SESSIONS_PER_USER`, what a scan costs (undo retention and TEMP on Oracle, purge lag and filesort on
  MySQL, §15's figures, `ds serve --no-worker` beside `ds worker` for cross-engine scans), a replica or
  standby; §9 (`ds target check`, `--no-worker`); §11 rows for P1 to P5 and F2 to F4, F8.
- **Prerequisites, word for word in DEPLOY §7 and README:** observed on MySQL 8.4.11 and
  Oracle 23.26.3 only (8.0 and 19c are supported, not observed: run the acceptance kit); Oracle over
  plain TCP or TCPS, not Native Network Encryption; in an Oracle database that is not AL32UTF8, a run
  takes text keys of up to 500 bytes, by a conversion observed on `NVARCHAR2` only; MariaDB and SQL Server
  are not supported; `ds target check` reports each of these before a run is attempted.
- **RUNBOOK:** "Compare two tables" gains "Across engines": §9's bridges; the first run (`ds target check`,
  one-key compares, a batch with `stop.maxKeys`, then the full scan); the acceptance kit, run by the site's
  DBA on its own 19c or 8.0; before a downgrade, finish or cancel runs with a MySQL or Oracle side and
  remove the engine targets from the targets file (the older release does not start while one is
  declared: its schema refuses the member); Troubleshooting.
- **RELEASE** §7, §14; **PACK** §1.2, §1.3, §2.2, §3.4; **SAMPLING** §3, §11.3 (the claim of a frame
  that was not read in a snapshot); **ALERTS** §2.1 (`error_type` of a `<class>: <code>` reason);
  **GRAPH** §1, §3; **LICENCE** §1, §8; **BENCHMARK** §2; **PENTEST** §2 (engine targets, the literal
  grammar, the guard, what is pinned and the redirect that is not, TLS enforcement, what the chat is
  told of a refused connect); **PLAN**; **README**; `SECURITY.md`.
- **docs/architecture.md:** §4.9 and §8.5 as built (a sorted
  merge with engine or worker digests); Appendix C rows.
- **`spec.schema.json`** (both copies): `typeSpec.emptyAsNull`. **`examples/tables/{mysql,oracle}/`:** seed
  SQL, targets, a spec and the expected result (the acceptance kit).

Each kit also has a README with its steps; it compares
two tables of one database (MySQL) or one schema (Oracle) through one target, one column of each type the
engine reads (on Oracle those of 19c: no `BOOLEAN`, no `JSON` type), with ten keys whose order a
collation would change and twenty seeded differences, and `expected.json` holds the scan's counts, the
worker's columns, the counters and outcomes `ds run` prints and each differing key with its paths. The
kits are observed on the two images only. TABLES §13 summarises §4 to §11 and refers here for the
observed facts and the full tables, rather than repeating them; it holds §15's measured figures whole.
DEPLOY §7's new part opens with the prerequisites; its compose layout for `--no-worker` is described,
not exercised by a test.

## 18. Limits and seams

**Limits.** Observed on MySQL 8.4.11 and Oracle Free 23.26.3 only. A first pass that cannot finish inside
Oracle's undo retention fails after three tries: a large table needs undo sized for the read, a standby or
a filter. In a non-AL32UTF8 Oracle database a text key column declared over 500 bytes is refused. A
listener that requires Native Network Encryption cannot be reached. A redirecting listener is trusted
(D12). A key the column cannot hold is skipped, not reported missing. A table with LOB columns scans in
batches of 4 rows or fewer on Oracle, and a row with over 16 MiB of LOBs is `resource_limit`. An Oracle
key before year 1 has no key text. A MySQL view's column that is itself a text made of a `DOUBLE`
(`CONCAT(d)`, `CAST(d AS CHAR)`) is a `varchar(22)` to the server, which cuts it in a batch as it cut
§9's cast (observed): such a view selects the `DOUBLE` itself. One filter text may select
different populations under two collations. The cancel of a sort is best effort: where the `KILL QUERY`
or the `cancel()` of §6 fails (the server unreachable, the account at its connection limit), the sort
runs to its end on the server. A MySQL server that answers a buffered statement with rows without end is
not bounded, and one that answers a count or a catalogue statement with rows of another shape is not
guarded (§14 point 6). A PostgreSQL pair with typeMap columns digests its batches on the event loop,
as before the seam: such a scan holds the API of `ds serve` as it did (§15). The PostgreSQL side of a cross-engine scan sizes a fetch by the one before
it, so the first rows of a run of much wider ones come at the earlier size. A per-key group of rows that
may hold LOBs can still hold 8 rows of 16 MiB a side, several times that in memory.
Cross-engine scans are slower than PostgreSQL pairs by a multiple §15 measures, and load the worker.

**Seams.** SQL Server: one more primitive set (bracket quoting, a binary UTF-8 collation, snapshot
isolation where allowed, else `none`, `HASHBYTES`) and a permissive driver. Files: built,
one more `ENGINES` entry, `files` ([FILES.md](FILES.md): CSV and Parquet in a mounted directory, read by
a DuckDB in a child process of its own, [READER.md](READER.md); object storage is still a seam there). Table pre-checks, built: `Engine.describe` with `nullable`, `size` and `key`, `relations`, `figures` and `duplicates` (§5). Schedules need nothing here.
Oracle flashback to an SCN (`snapshot(at=...)`, needs the FLASHBACK grant). `NCHAR` and `NVARCHAR2` keys
(the order expression already serves them). Client certificates. Vetting `dsn_ref` targets. Refusing a
pack whose side a was not read in a snapshot.
