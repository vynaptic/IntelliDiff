# Security

How vulnerabilities in DeltaSleuth are handled, and how you verify and scan a release. This file
is at the root of every signed release bundle and of its kit. The maintainer builds and signs a release
with `tools/release.py`; installing, updating and backing up are in
[docs/deployment.md](docs/deployment.md).

## 1. Supported versions

The latest minor version, and the previous one for 6 months after its successor's release.

## 2. Reporting a vulnerability

Write to `<SECURITY-CONTACT>`, a role address (a placeholder, to be filled in: the maintainer names the
address before a release, and `tools/release.py build` refuses while the placeholder remains). Include
the version, the `config` line of the release's `RELEASE.txt` (or
`docker image inspect --format '{{.Id}}' deltasleuth:<version>`) and the steps to reproduce. Never send
production or personal data. An encrypted channel is available on request. Research in good faith,
within these terms and without harm to anyone's data or service, has safe harbour: we will not pursue
it.

## 3. Targets (proposed)

Severity is the CVSS v4.0 base score (and v3.1), adjusted for the documented deployment with written
reasons. The clock starts when we confirm a report, or, for a dependency, once a fix exists and
DeltaSleuth is affected. A dependency finding that does not affect DeltaSleuth gets `not_affected` in
the next release's VEX, or a signed interim VEX within 10 business days of your request.

| Severity | Acknowledge | Assess | Fixed release or mitigation | Advisory |
|---|---|---|---|---|
| Critical | 2 business days | 5 business days | 14 days; guidance within 72 h | at the fix; within 24 h of confirmed exploitation |
| High | 2 business days | 5 business days | 30 days | at the fix |
| Medium | 2 business days | 10 business days | 90 days | release notes |
| Low | 5 business days | — | next minor, ≤ 180 days | release notes |

## 4. Disclosure

Coordinated: sites that run a release are told first. The embargo lasts until the fix plus 14 days, and
at most 90 days. Credit is given unless declined. We request CVEs through a CNA for our own Critical and
High findings.

## 5. Advisories

`DS-SA-YYYY-NNN.txt`, whose first line is `# DeltaSleuth security advisory DS-SA-YYYY-NNN`: a summary,
the affected versions and `config`s, severity and vector, CVEs, the fixed version and its list digest
(`sums_sha256`), mitigations, VEX and credit. Each is signed with the release key into
`DS-SA-YYYY-NNN.txt.sig` and sent to your named security contacts; Critical ones also by phone. Check
it, or a signed interim VEX, with steps 1 and 2 of the procedure, against the `release-key.pem`
of a bundle you verified. Accept it only if step 1 prints a fingerprint your register lists as
`signing` or `standby` on the day it arrives, never a retired or withdrawn key's: older bundles still
carry those keys' `release-key.pem`, and a withdrawn key may be in other hands.

```sh
openssl pkey -pubin -in release-key.pem -outform DER | sha256sum    # signing or standby in your register today
openssl dgst -sha256 -verify release-key.pem -signature DS-SA-2026-001.txt.sig DS-SA-2026-001.txt   # Verified OK
```

The compromise advisory is the exception: it is never signed with the stolen key, and you confirm
it by phone, where we read back its sha256 (`sha256sum DS-SA-YYYY-NNN.txt`).

## 6. Fixes, air-gapped

A fix is always a complete signed bundle, never a delta. Verify it on the Docker host, take a backup,
then install it ([docs/deployment.md](docs/deployment.md) has the backup and the update); the maintainer
never needs remote access.

## 7. Verifying a release

Each bundle's `SHA256SUMS` is signed with a release key; `VERIFY.txt` names the release, then repeats the
procedure you filed with the key. Run the filed copy, all of it, on the Docker host. The release keys:

| Fingerprint (step 1) | Status | procedure= (sha256 of the filed `procedure.txt`) |
|---|---|---|
| none yet | — | — |

A status is `signing`, `standby`, `retired YYYY-MM-DD` or `withdrawn YYYY-MM-DD`. A `signing` or
`standby` key is in service; a `standby` key signs no bundle: it is the next key during a rotation, or
an offline spare.

**What the signature proves:** "The signature proves that the holder of the release key with
fingerprint <fp> issued these files unchanged: the image, its SBOMs, the scan results and the source it
was built from. It does not prove that the build machine was uncompromised, that the image is free of
vulnerabilities (scan/ lists what was known on the release date), that the source rebuilds the same
image bytes (builds are not reproducible), or that the key was not stolen. A bundle signed by a
withdrawn key is genuine only if its list digest (step 4) appears in that key's compromise advisory."
(`<fp>` is the fingerprint step 1 prints.)

**Rotation,** every two years or on suspicion, in this order:
1. We list the new key as `standby`, beside the old `signing` one.
2. The old key signs `transition.txt` (`old:`, `new:` and `from:` lines) into `transition.txt.sig`.
   Its last bundle ships both, listed in its `SHA256SUMS`, and its `SECURITY.md` lists the new key as
   `standby`.
3. Only then does the new key become `signing` and the old one `retired YYYY-MM-DD`: every later
   bundle is the new key's, and the old key signs nothing more.

Once the old key's last bundle passes the procedure, check the transition with the same
`release-key.pem`, `openssl dgst -sha256 -verify release-key.pem -signature transition.txt.sig
transition.txt` (`Verified OK`), confirm the new fingerprint with us by phone, file it, and end-date
the old entry but keep it: older bundles still name it.

**Loss** of the key means a new key, filed at every site like the first.

**Compromise:** we stop releasing and send an advisory, never signed with the stolen key; confirm it by
phone, where we read back its sha256. It gives the date and every genuine list digest of that key; a
bundle of that key whose digest it does not list is not genuine. A new key is filed at every site, the
old one becomes `withdrawn`, and the supported versions are re-released as patches.

## 8. Scanning our releases

The bundle's `scan/` was right on its release date. We re-scan every supported release weekly, with a
current database and our triage: a finding that would block a release, one with a fix
that is Critical, High, of unknown severity or known exploited, starts section 3's clock. To scan it again
yourselves, air-gapped, fetch elsewhere the grype database named by `from` in `scan/SUMMARY.txt`, or a
newer one from `grype db list`, bring it across, `grype db import` it, and run in the bundle's folder:

```sh
GRYPE_DB_AUTO_UPDATE=false GRYPE_CHECK_FOR_APP_UPDATE=false GRYPE_DB_VALIDATE_AGE=false grype sbom:sbom/<stem>.cdx.json --vex scan/vex.openvex.json
```

Other scanners, Trivy and Dependency-Track among them, read the SBOMs (CycloneDX 1.6 and SPDX 2.3).
Whether Trivy applies our VEX is untested, and Dependency-Track cannot import OpenVEX. Expect Debian
`wont-fix` findings, the libraries wheels vendor in binary scanners (`sbom/vendored-libraries.txt` gives
our decision on each), and newer findings: report those to us.

## 9. Pen testing

An independent firm tests a named signed release, or a release candidate, installed from its bundle with
the verification of section 7 included. Its report names the version and the image's `config` digest; a
finding on another build is re-checked on that one.

## 10. By design, not vulnerabilities

- Membership of the Docker group is root-equivalent on the host: a member can run any `ds` command as
  any operator.
- This build enforces no licence: it is an open build, which embeds no vendor key, and a release's
  `NOTES.txt` says so on a line of its own:
  `This release is an open build: it embeds no licence vendor key and enforces no licence.` In a vendor
  build, which embeds keys, the licence is a contractual control, not a technical one.
- Developer mode (no `auth` settings) serves loopback only, and every local user acts as admin.
- Notifications go only to the channels the settings declare, and those routed through Slack or
  Teams leave the site's network for that service: a notification carries ids, counts, closed codes, a
  spec name (off by default for Slack and Teams) and a link, never a key or value. Every address a send
  reaches is checked (link-local and metadata addresses never, private ones only with the channel's
  opt-in); behind `notify.proxy` only the declared host is checked, and the proxy resolves the rest.
- The MCP server, which is off unless the settings have an `mcp` block: a
  tool result reaches the model host of the agent that asked for it, a host the installation does not
  run and DeltaSleuth's LLM egress policy does not govern; what a result may carry is the settings'
  `mcp.data_classes`, and retention, purge and erasure do not reach an agent's copies. Masked forms are
  stable pseudonyms, not anonymous. Text planted in a payload or a name can fool an agent. Over HTTP
  `mcp.data_classes`, `mcp.work`, the absence of run control, the rate limits and the `mcp.read` audit
  rows hold for calls made through `/v1/mcp` only: the bearer is a general API token, so a fooled agent
  that can read its token acts on `/v1` with the token's full role (an `operator` token starts, resumes
  and cancels runs whatever `mcp.work` says; a `viewer` token reads clear-declared values there
  whatever `mcp.data_classes` says). The token's role is the bound, which is why an agent gets a
  `viewer` token. Over stdio (`ds mcp`) a fooled agent acts with the OS account that runs the command,
  which is why that command is a workstation's in developer mode and, where the settings have `auth`,
  the host-administrator channel behind `--host-channel`. DeltaSleuth checks no answer an external
  agent writes. The server takes an API token, has no OAuth, adds no outbound destination and sends a
  client no request; where `mcp.work` lists it, `compare_key` reads the run's two declared targets as
  any compare does, at most 10 times a minute per caller.

- A MySQL or Oracle target: every session goes to an address
  DeltaSleuth resolved and vetted, with one exception by design: an Oracle listener may redirect a
  session and the driver follows it, so an Oracle target is only as contained as its listener and the
  egress rule around it. An IPv6 address that stands for an IPv4 one (IPv4-mapped, the well-known
  NAT64 prefix) is vetted as that one; a network's own NAT64 prefix is not known to the guard. A
  PostgreSQL `dsn_ref` target's host is inside its secret DSN and is not
  vetted. TLS is required unless the target declares `allow_unencrypted`; `tls_verify_name: false`
  accepts any certificate the declared CA issued. A connect that is refused names the target's declared
  host, port, service or database and account, never the password, to whoever creates the run or runs
  `ds target check`; the chat's model is told the side and the target id only. On MySQL, key values, filter literals and resume
  bounds are written into statement text as hexadecimal digits, which that database's logs may keep.
  Values are compared as the account reads them: a database that masks or redacts both sides alike
  reads as a match, and the certificate says only whose view of the rows was compared. That the account
  cannot write is its grants' doing, not DeltaSleuth's.
- A files target: CSV and Parquet
  files are parsed by DuckDB, native code, locked to the declared directories and its own spill and
  running nothing but single `SELECT` statements DeltaSleuth composes. DuckDB runs in a child process
  of each `ds` process, the file reader, and in no process that holds the keyring: a fault or a memory
  blow-up of DuckDB's ends that child, another is started, the run pauses, and files on which the
  reader ended three times while they alone were being read are read no more until a restart. **What a
  site is told:** on every platform the reader's end is the reader's alone, and no core is left; on
  Linux (observed natively on aarch64, in an image built from source) the reader is also not dumpable,
  is the process the kernel's memory killer takes first, sets itself a data limit and denies itself
  sockets (a seccomp filter), unless `files.os_limits` is false; its start's log line and `ds target
  check` print what held on that host. The service takes the reader's answers as frames of a fixed
  form, each checked and bounded before anything is built of it (its size, its cells, its width, the
  types a plan keeps or words), and one outside that form ends the reader; what text of the reader's
  reaches a refusal or a log line passes a grammar of the service's own (its version, a fault's class,
  a column's type), but a column's name, which is the file's and is quoted (`ds target check` prints a
  file's names and its sniffed dialect, on its own output). **What is
  never claimed:** that rows of the right form are the files' rows, should a crafted file run code in
  the reader; that the reader is a sandbox or
  keeps anything from the user it runs as (it is the service's own user: it can open the settings, a
  secret file, the evidence's ciphertext and other exports, and can signal its parent); that the
  process which holds the keyring parses no byte of a file (DeltaSleuth's own walk of a Parquet footer
  and its check of a `gzip` or `zstd` stream do, there); that a crafted file is harmless (it can end
  the reader, cost other runs a pass, and fail its own run); a data limit or denied sockets on macOS,
  or on any host whose start line does not print them; and anything of an x86_64 kernel, on which the
  filter and the limit were not observed before a site's own self-test, the command
  `docker compose run --rm app python -c "from deltasleuth.connectors.tables import duck; duck.selftest()"`.
  Against a writer of the
  export directory who can craft Parquet bytes, this much is bounded, by DeltaSleuth's own walk of each
  Parquet footer before DuckDB sees the file: the nesting
  of its schema (DuckDB follows it without a bound, and a deeply nested one ended the process), what
  its lists make DuckDB's reader hold (a footer of 1 MiB took 672 MiB outside `files.memory`; a row
  group's column chunks must be the schema's leaves, with at most 10,000 row groups, 200,000 column
  chunks and 2,000,000 list elements, which held the reader to 235 MiB a footer and, in one
  statement over many files, to about 2.5 such footers a thread of `files.threads` (965 MiB at 2
  threads, 2.2 GiB at 4, 4.7 GiB at 8), as measured on macOS arm64), and the numbers DuckDB read as an internal error
  (negative lengths, scales, precisions and row counts; an encrypted column). No statement reads a
  footer's statistics as text any more, which ended the process on one crafted statistic, a time's text
  is built without DuckDB's own conversion, which ended it on a time outside a day, and the per-key
  read holds no predicate that statistics prune. Of 250,000 footers with a few changed bytes, the
  40,566 the walk accepted ended no process: a test, not a proof. **Not bounded:** what DuckDB's
  decoders do with a file's pages. A crafted page is known to end the process that decodes it (a
  `DELTA_BINARY_PACKED` block size of 0, found by changing page bytes: one file of 35,907): since the
  hardening that is the reader's process, and the service goes on (observed on macOS arm64 and on
  linux/arm64). Run again through the reader, 40,000 accepted footer mutants and 40,000 page mutants
  ended no process at all: a test, not a proof. Under a spec's filter DuckDB trusts a column's
  statistics, so a row group they exclude is not read. DuckDB opens files by path,
  so whoever can write the export directory on the host can swap a file for a link between a check and
  a read and have the service read another file it may read into encrypted evidence; the next check
  fails the run. A parent directory swapped for another and put back between two checks is seen by no
  check, the closing hash included, since the files themselves did not change: the service then
  compared other bytes than the digest on the certificate names, and only reads pinned by an open
  descriptor, which are not built, would close that. A sort's spill is plaintext on the evidence volume
  (or the volume `files.spill_dir` names) for the length of a statement, and until the next sweep after
  a crash. An evidence pack of a run with a file side carries one unkeyed SHA-256 over its files (of
  the list of their SHA-256 values, never a file's own: the pack says how it is formed), by which a
  holder of the files (or a guesser of a small export) can confirm what was compared. Each matched
  file's name, size, times and own digest stay sealed under the run's data key; the spec's own path, the
  count of a side's files, their total bytes and the newest modification time are in the plan, the
  pack and the API's answers in clear, so a side of one file discloses that file's size and time, and
  its path where the spec names it. DuckDB's compiled-in third-party libraries are in no SBOM entry
  and no scan.

## 11. Scope

The image, the deployment kit, the documentation and the advisories. Not a site's infrastructure,
PostgreSQL, the MySQL and Oracle databases a target names, the exported files and the mounts a files
target names, the identity provider, LLM providers,
notification receivers (webhook endpoints, SMTP relays, Slack, Teams) or an agent's MCP client and the
model host it uses.

We keep a private register of every report and finding: its id, source, component, severity, dates,
affected and fixed versions, and VEX status.
