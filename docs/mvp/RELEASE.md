# Signed releases, SBOM and the vulnerability process: contract

This contract is binding. It builds signed releases, using the signature style of
[PACK.md](PACK.md) §3 and [LICENCE.md](LICENCE.md) §3–§4.
SOC 2 is out of scope.

**Goal:** a site installs DeltaSleuth, air-gapped, from files the maintainer signed, and checks them with
OpenSSL or LibreSSL and sha256sum before `docker load`. Each release carries its SBOM, its scan and
its triage. A published process covers vulnerabilities, and the pen-test vendor gets a scope.

**Shape:** `tools/release.py` is dsv's sibling, on the maintainer's side. It:
1. builds the image from a git archive of HEAD, and saves it;
2. catalogues it with syft and scans it with grype and pip-audit;
3. gates the result;
4. signs `SHA256SUMS` with a release key that never leaves the maintainer's machine;
5. tags the commit.

Source builds stay possible, unsigned. Nothing new goes on GitHub, and nothing calls out.

**Threat model:** the signature defends the path from the maintainer's machine to the site's Docker host
against swapped, edited or added files and a forged procedure, provided the whole procedure runs on
that host (§3). It does not defend a compromised build machine, which builds, scans and signs. Builds
are not reproducible, so sites rely on the signature and the SBOM, not on a rebuild.

## 1. Decisions

**Fixed (binding).**
- **O1:** signed image tarballs with their SBOM and checksums, verified with stock tools, then
  loaded; air-gapped. Source builds stay possible, unsigned.
- **O2:** the two base images, pinned by digest; syft 1.52.0 and grype 0.119.0; pip-audit as a dev
  dependency; SBOM formats that sites recognise.
- **O3:** one local script, nothing new on GitHub, and a release key that never leaves the maintainer's
  machine.

**This contract's:**
- **D1** What is signed is the gzipped `docker save` bytes; `RELEASE.txt` binds the config and manifest
  digests.
- **D2** One bundle per platform, `linux/amd64` by default. A release builds from the platform's
  manifest digest of the pinned python index, so the local index reference never changes (§5).
- **D3** The build uses `git archive HEAD -- $SHIPPED`, never the working tree, and that archive also
  ships as the deployment kit.
- **D4** CycloneDX 1.6 and SPDX 2.3, with a completeness check that covers Debian, the python binary,
  PyPI and npm. Wheel-vendored libraries get a dated decision per file.
- **D5** Findings that have a fix and are Critical, High, unknown severity or KEV block the release,
  unless triaged with an expiry. Triage becomes OpenVEX.
- **D6** uv moves to a build stage, pinned by hash. pip is uninstalled, and the user is numeric.
- **D7** Compose runs `deltasleuth:<version>`, `pull_policy: never`, with no build. Every site change
  lives in the untracked `deploy/docker-compose.site.yml`, named in `.env`, which no upgrade
  overwrites. The source and CA layers use other tags.
- **D8** `app` and `mocks` are hardened. `db` is unchanged, and resource limits are a site choice.
- **D9** The tag is created only after success, on the built revision. A patch release adds no
  migration.
- **D10** There is one release key; a standby on offline media, **O-standby**, is the maintainer's call,
  since it relaxes O3. A rotation's next key is listed `standby` too, until the old key's last bundle
  ships (§4).
- **D11** The release key also signs advisories and key transitions.
- **D12** The pen-test lab uses a hosted IdP tenant, because the fake IdP has no authorize page and
  `config._issuer` allows http only on loopback.
- **D13** syft, grype and pip-audit run at the approved versions only, with an empty config file and
  an environment built from scratch.

## 2. The bundle

`build --out DIR` writes the folder `<stem>/`, where `<stem>` is `deltasleuth-<v>-<os>-<arch>`, for
example `deltasleuth-0.1.0-linux-amd64`. Beside the folder it writes a transport tar of it (ustar,
sorted, regular files and folders only, modes 0644 and 0755, uid and gid 0 with no names, commit
mtime). The tar's sha256 is printed for the transfer ticket. The tar itself is unsigned, so the site
checks it before extracting (DEPLOY §2).

| Path | Content |
|---|---|
| `VERIFY.txt` | two release lines, then the canonical procedure (§3) |
| `RELEASE.txt`, `NOTES.txt` | below; `--notes FILE` verbatim, also the tag message |
| `SECURITY.md` | the repository's (§11), which VERIFY.txt and the rotation steps cite |
| `transition.txt`, `transition.txt.sig` | only in the old key's last bundle (§4) |
| `image/<stem>.tar.gz` | `docker save deltasleuth:<v>`, Python `GzipFile(filename="", mtime=0)`, level 9 |
| `sbom/<stem>.cdx.json`, `.spdx.json`, `vendored-libraries.txt` | §7 |
| `scan/grype.json` | before VEX; `descriptor` cut to `name`, `version`, `db.status` without `path` (no host path ships) |
| `scan/pip-audit.json`, `vex.openvex.json`, `SUMMARY.txt` | §8; the summary gives policy, result, counts by severity and fix state, triaged findings, vendored decisions, the database's `built` and `from`, and rescan commands |
| `source/deltasleuth-<v>-src.tar.gz` | `git archive --format=tar --prefix=deltasleuth-<v>/ HEAD -- $SHIPPED`, gzipped the same way |
| `SHA256SUMS`, `.sig`, `release-key.pem` | unlisted, as in packs: sorted coreutils lines of every other file; DER ECDSA P-256/SHA-256 (`PackSigner.sign`); SPKI |

`SHIPPED = README.md SECURITY.md pyproject.toml uv.lock deltasleuth web deploy docs/mvp docs/guide mocks examples`
leaves out `docs/architecture*`, `docs/bench`, `tests` and `tools`; `docs/guide` is the
user guide ([FIRSTRUN.md](FIRSTRUN.md) §10). Git-ignored site
files (`.env`, `deploy/docker-compose.site.yml`, `deploy/config/*.yaml`, `deploy/secrets/`) never
reach it. `RELEASE.txt` holds these lines, in this order:

```text
format: deltasleuth-release/1
version: 0.1.0
revision: <40 hex>
created: <commit time, YYYY-MM-DDTHH:MM:SSZ>
platform: linux/amd64
image: deltasleuth:0.1.0
config: sha256:<64 hex>
manifest: sha256:<64 hex>
base: docker.io/library/python@sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332 (linux/amd64 of python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b)
build-base: docker.io/library/node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1
vendor: <pyproject [project].authors[0].name>
key: <fingerprint>
signed: <UTC>
sbom: syft <version syft reported>; CycloneDX 1.6, SPDX 2.3
scan: grype <version grype reported>, database <schema> built <UTC>; pip-audit <version>
gate: passed; <n> blocking, <n> triaged, <n> reported, <n> vendored file(s) decided
```

- **Digests:** read from the saved tar (`index.json` → manifest → `config.digest`). The archive must
  hold exactly one manifest and one `manifest.json` entry, tagged `deltasleuth:<v>`.
- **Deterministic for a given image:** `docker save` (two saves were identical), the gzip,
  `git archive` (identical twice), the sums, the signature and the transport tar.
- **Not deterministic:** the build, the SBOMs and the scans.

## 3. Signing and verification

`SHA256SUMS` lists every file, so the signature binds the tarball bytes. Through them it binds the
`index.json`, the manifest and the config that `docker load` reads, and `docker load` checks each layer
against `diff_ids`. There is no registry digest: an air-gapped site has no registry, and verifying
comes first.

`keygen` writes the canonical procedure as `procedure.txt`, and its sha256 goes on the key's line
(§4). It travels out of band once per key, and the site files it as `FILED-RELEASE-PROCEDURE.txt`
beside the bundle. `VERIFY.txt` is three parts:
- line 1: `DeltaSleuth release 0.1.0 for linux/amd64. In its folder, run in sh the copy of the
  procedure below filed with the release key; its step 0 reads line 2:`;
- line 2: `VERSION=0.1.0 PLATFORM=linux-amd64`;
- then `release.PROCEDURE`:

```text
# deltasleuth-release/1 verification
Needs OpenSSL or LibreSSL, POSIX tools, sha256sum (or: sha256sum() { shasum -a 256 "$@"; }) and, for step 5, Docker. Run every step on the Docker host.
0. Use the copy filed with the release key; VERIFY.txt must match it from its line 3 (no output):
   sed -n '3,$p' VERIFY.txt | diff - ../FILED-RELEASE-PROCEDURE.txt
   Line 2 names this release; never run it: this checks it and sets its values (no output):
   F='VERSION=[0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)? PLATFORM=linux-(amd64|arm64)'
   L=$(sed -n 2p VERIFY.txt | LC_ALL=C grep -Ex "$F") || echo "line 2 of VERIFY.txt is malformed"
   v() { printf '%s\n' "$L" | cut -d' ' -f"$1" | cut -d= -f2; }
   VERSION=$(v 1) PLATFORM=$(v 2)
1. The key: prints a fingerprint your register lists as a DeltaSleuth release key, in service on step 4's signed date (a withdrawn key: only if step 4's digest is in its compromise advisory).
   openssl pkey -pubin -in release-key.pem -outform DER | sha256sum
2. It signed the list:
   openssl dgst -sha256 -verify release-key.pem -signature SHA256SUMS.sig SHA256SUMS  -> Verified OK
3. Files unchanged, none added (the grep prints nothing; every line OK; diff prints nothing):
   LC_ALL=C grep -vE '^[0-9a-f]{64}  [^/]' SHA256SUMS
   sha256sum -c SHA256SUMS
   find . -type f | sed 's|^\./||' | LC_ALL=C sort > ../have.txt
   { cut -c67- SHA256SUMS; printf '%s\n' SHA256SUMS SHA256SUMS.sig release-key.pem; } | LC_ALL=C sort > ../want.txt
   diff ../want.txt ../have.txt
4. The release: match it with your change record, and file the list's digest there:
   grep -E '^(version|revision|created|platform|image|config|manifest|key|signed): ' RELEASE.txt
   sha256sum SHA256SUMS | cut -c1-64
5. Only now: load the image; its tag must name the image the release lists (prints 1):
   docker load -i "image/deltasleuth-$VERSION-$PLATFORM.tar.gz"
   docker image inspect --format '{{.Id}}' "deltasleuth:$VERSION" > ../id.txt
   grep -E '^(config|manifest): ' RELEASE.txt | cut -d' ' -f2 | grep -cxF -f ../id.txt
What a valid result proves: SECURITY.md, "Verifying a release".
```

**Rules, as in PACK:**
- The filed copy is authoritative: commands are typed from it, never from `VERIFY.txt`. No verifier
  script ships, and the procedure reads output, not exit codes.
- **The whole procedure runs on the Docker host,** in one sitting, in the folder it loads from. A pass
  elsewhere (a quarantine workstation) does not count: on the way to the host, the image,
  `RELEASE.txt` and `SHA256SUMS` can be swapped together, and only step 2 catches that.
- Step 5 accepts the config digest or the manifest digest:
  - the classic store's `.Id` is the config digest (checked on Docker 29.1.3);
  - the containerd store, the default of fresh Docker Engine 29 and recent Docker Desktop installs,
    should give the manifest digest: the saved `index.json` names the manifest, which containerd
    keeps as the image's target. This is untested (§15).

**Observed** with OpenSSL 3.6.2 and LibreSSL 3.3.6, each under `sha256sum` and under
`shasum -a 256`, through `packs.steps()` unchanged (steps 0–4). The untouched bundle passes. Each
tamper fails at the step shown:
- a flipped image byte, or an added or removed file: step 3;
- an edited `SHA256SUMS`: step 2;
- a signed list with a line that is not a checksum line (an advisory's shape, which GNU sha256sum and
  shasum skip and macOS's `/sbin/sha256sum` only warns about): step 3's grep prints it;
- after a pass elsewhere, a consistent unsigned swap of the image, `RELEASE.txt` and `SHA256SUMS`:
  step 2 on the host;
- another key's re-signature: step 1;
- that key's block, with a header line, before the genuine one: step 2 (OpenSSL 3 prints
  `Verification failure` although step 1 showed the genuine fingerprint; LibreSSL loads no key);
- a line before the header, or shell in line 2: step 0, which never runs it;
- another loaded id: step 5 prints 0.

**SECURITY.md, "What the signature proves" (verbatim):** "The signature proves that the holder of the
release key with fingerprint <fp> issued these files unchanged: the image, its SBOMs, the scan results
and the source it was built from. It does not prove that the build machine was uncompromised, that the
image is free of vulnerabilities (scan/ lists what was known on the release date), that the source
rebuilds the same image bytes (builds are not reproducible), or that the key was not stolen. A bundle
signed by a withdrawn key is genuine only if its list digest (step 4) appears in that key's compromise
advisory."

## 4. Release keys

- **Generation.**
  - dsv's helpers become public (`fail`, `file_ref`, `passphrase`, `create`, `signer`). Its keygen
    body becomes `dsv.keypair(out, prefix, passphrase) -> PackSigner`, which dsv calls with
    `"vendor"`. dsv's output and tests are unchanged.
  - `release.py keygen` writes `release-<fp16>.key.pem` (P-256, PKCS#8, `BestAvailableEncryption`,
    0600), `.pub.pem` and `procedure.txt`, never over a file.
  - It refuses a directory inside a git working tree or a cloud-synced folder: after resolving
    symlinks, under `~/Desktop`, `~/Documents`, `~/Library/Mobile Documents` or
    `~/Library/CloudStorage`, by name or by file identity (APFS ignores case, so `~/documents` is
    `~/Documents` and is refused too). `build` refuses a `--key` or `--passphrase` file there too.
  - It prints the fingerprint, the `tools/release-keys.txt` line, the SECURITY.md row and
    `tmutil addexclusion <dir>`.
  - The editable install puts the repository root on `sys.path`, so `from tools import dsv` works.
- **Custody.** The key stays on the maintainer's machine, outside every repository and synced folder, in a
  0700 directory, on FileVault, excluded from Time Machine (`tmutil addexclusion`). Any encrypted
  backup is the maintainer's recorded decision. The passphrase comes by prompt or `file:`, never from the
  environment, argv or chat.
- **List.** `tools/release-keys.txt` has one line per key: `<fp> <status> procedure=<sha256>`. The
  status is `signing`, `standby`, `retired YYYY-MM-DD` or `withdrawn YYYY-MM-DD`; a `signing` or
  `standby` key is in service, and a `standby` key (O-standby, or a rotation's next key) signs no
  bundle. SECURITY.md's table mirrors it. `build` signs only with the `signing` key, and refuses when
  `release.PROCEDURE` no longer hashes to that key's `procedure=`. A changed procedure is a new format
  (`deltasleuth-release/2`), filed at every site like a new key. `sign` refuses a key the list marks
  `retired` or `withdrawn`.
- **Separation.** `build` and `sign` refuse a key in `licence.SHIPPED_KEYS` or `REMOVED_KEYS`. A test
  keeps the list disjoint from `tests/fixtures/vendor-keys.txt`. dsv refuses any key outside
  `VENDOR_KEYS`, and pack keys belong to the sites.
- **Publication.** Once per key and site, out of band: the
  fingerprint and `procedure.txt`. A call reads back the whole fingerprint, as the site's register
  records it, and the site files both, dated.
- **Register.** `build --register FILE` appends
  `{version, platform, revision, created, signed, key, config, manifest, sums_sha256}` before signing,
  as `dsv issue` does.
- **Rotation** (every two years, or on suspicion):
  1. Generate a new key into a new 0700 folder (keygen refuses one that holds the old key's
     `procedure.txt`), and list it as `standby` (keygen prints `signing`) in `tools/release-keys.txt`
     and SECURITY.md's table; the old key stays `signing`.
  2. The old key signs `transition.txt`: `# DeltaSleuth release key transition`, `old: <fp>`,
     `new: <fp>`, `from: YYYY-MM-DD`.
  3. The transition and its `.sig` ship, listed, in the old key's last bundle, whose SECURITY.md
     already lists the new key. The maintainer commits them as `tools/transition.txt` and
     `tools/transition.txt.sig` (`sign` writes the `.sig`); `build` ships them when `old:` names its
     signing key, and refuses a `.sig` that key did not make.
  4. Only then does the new key become `signing` and the old one `retired YYYY-MM-DD`, committed;
     from then on `build` and `sign` refuse the old key.
  5. Sites verify it, confirm by phone, and end-date the old entry but keep it. Then the old private
     key is destroyed.
- **Loss:** a new key, with the full ceremony at every site, unless O-standby exists (as LICENCE §4's
  K2).
- **Compromise:**
  1. Stop releasing.
  2. Send an advisory, never signed with the stolen key, and read its
     sha256 back to each site by phone. It gives the date and every genuine `sums_sha256` from the
     register; a bundle of that key whose digest it does not list is not genuine.
  3. Hold the new-key ceremony, and mark the old key `withdrawn` (then `sign` refuses it).
  4. Re-release the supported lines as patches.

## 5. The image (`deploy/Dockerfile`, exact)

```dockerfile
# DeltaSleuth: Node builds the UI and uv the Python environment; the runtime stage holds neither and runs
# `ds serve` as uid 10001. Base images are pinned by digest (docs/mvp/RELEASE.md §5): change one on
# purpose, with tools/release.py's INDEX and BASES. Releases: tools/release.py. From source (unsigned):
#   docker build -f deploy/Dockerfile -t deltasleuth:source .

# The python:3.13-slim index; a release passes its platform's manifest digest instead (release.BASES).
ARG PYTHON=python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

FROM --platform=$BUILDPLATFORM node:24-alpine@sha256:ebfe2f90462722a7a4de65e91990e97fe0d401c70e0e762c5b53302f905ec1c1 AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${PYTHON} AS venv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY deploy/uv-requirements.txt /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/uv-requirements.txt
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
# Dependencies first, so this layer survives code changes. --no-cache: the image keeps no download cache.
RUN uv sync --frozen --no-dev --no-install-project --no-cache
COPY deltasleuth/ deltasleuth/
RUN uv sync --frozen --no-dev --no-cache

FROM ${PYTHON}
ENV PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH DS_CONFIG=/etc/deltasleuth/deltasleuth.yaml
RUN python -m pip uninstall --yes --root-user-action=ignore pip \
 && groupadd --system --gid 10001 deltasleuth \
 && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin deltasleuth \
 && install -d -o deltasleuth -m 0700 /var/lib/deltasleuth/evidence /var/lib/deltasleuth/workspace \
      /var/lib/deltasleuth/p0 /var/lib/deltasleuth/files /var/lib/deltasleuth/spill
WORKDIR /app
COPY --from=venv /app ./
COPY --from=web /src/web/dist web/dist/
# What web/dist bundles, for the SBOM (RELEASE.md §7); outside web/dist, so never served.
COPY web/package-lock.json web/
USER 10001:10001
EXPOSE 8000
# (the HEALTHCHECK and its comment, unchanged)
CMD ["ds", "serve", "--host", "0.0.0.0", "--port", "8000"]
# Last, so that a new version rebuilds no layer: tools/release.py sets them, a source build keeps these.
ARG VERSION=source REVISION=unknown CREATED=unknown VENDOR=unknown
LABEL org.opencontainers.image.title=DeltaSleuth org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION org.opencontainers.image.created=$CREATED \
      org.opencontainers.image.vendor=$VENDOR org.opencontainers.image.licenses=Apache-2.0
```

- **Platform manifests.** `release.INDEX` is the index digest above. `release.BASES` holds its
  platform manifests, looked up, not pulled:
  - `linux/amd64`: `sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332`;
  - `linux/arm64`: `sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f`.

  `build` passes `--build-arg PYTHON=python@sha256:<BASES[platform]>`.
- **Why platform manifests.** In the classic store, one reference names one image. A
  `docker pull --platform linux/amd64` of the index would re-point it, and then every native arm64
  source build would silently pull again, flipping it back. A manifest digest is a reference of its
  own, so the index keeps naming the local arm64 image, and source builds pull nothing. The containerd
  store was rejected: it starts empty, so every base and postgres would be pulled again.
- **`deploy/uv-requirements.txt`** holds `uv==0.11.21` with a `--hash=sha256:` for each manylinux x86_64
  and aarch64 wheel. `Dockerfile.dockerignore` gains `!deploy/uv-requirements.txt`. PyPI lists three
  such wheels: `manylinux_2_17` x86_64, `manylinux_2_17` aarch64 and `manylinux_2_28` aarch64, which
  pip takes on arm64 (checked).
- **`release.PROCEDURE`** is read from `tools/release-procedure.txt`, as `pack.PROCEDURE` is from
  `templates/pack-procedure.txt`: its lines exceed ruff's 110 characters.
- **No `# syntax=` line,** because that line would pull an image.
- **Node** runs on the build platform, since `web/dist` does not depend on the architecture.
- **Both python stages share `${PYTHON}`,** so the venv and the editable install hold at `/app`
  (`WEB_DIST` is `parents[2]`).
- **Removed:** uv, uvx and their 1,012 Rust crates, including the High GHSA-4w2j-m93h-cj5j (quinn-proto).
  pip leaves the final filesystem, but its base-layer files and ensurepip's wheel remain.
- **Existing regexes** (the `install -d` line, the HEALTHCHECK) still match.
- **The file reader's process** ([READER.md](READER.md)) changed nothing here: no line of the Dockerfile
  and no compose file. The reader is a second process of the image's interpreter (`python -I -X utf8
  /app/deltasleuth/connectors/tables/reader.py`), started by `ds` as uid 10001 under the same flags; it
  needs no capability, no writable root and no executable `/tmp`, and `ds serve`, PID 1 in the
  container, reaps it. The image runs no init process: a `ds` command run beside the service (`docker
  compose exec`) kills and reaps its own reader as it exits, and one that is killed leaves its reader
  as a defunct entry until the container stops (READER §17). On 2026-10-04 this Dockerfile was built
  from the working tree for the
  reader's image tests (§13), never as a release: natively for linux/arm64 (`deltasleuth:reader`, 716
  MB) and, with `--platform linux/amd64 --build-arg PYTHON=python@sha256:<BASES["linux/amd64"]>`, for
  linux/amd64 (`deltasleuth:reader-amd64`, 690 MB). Both bases were in the local store, so nothing was
  pulled; the builds downloaded the wheels `uv.lock` pins for each machine (DuckDB's manylinux wheels
  among them), `uv` and the npm packages. The linux/arm64 image was later built
  again from the working tree, under the same tag; the amd64 one was not.

## 6. Compose and hardening

**The base file.** `deploy/docker-compose.yml` loses `x-build`, both `build:` keys and the header's
`--build` and `-f` (its commands read `.env`, and it says never to edit it). `app` and `mocks` start
with `<<: *release`, and `db` is unchanged. `app.secrets` keeps `[db_dsn, kek, tokeniser]`. The optional secrets (`oidc_client_secret`, `llm_key`) move to the site
file: a list in an override adds to the base list, and removing needs `!reset`.

```yaml
x-release: &release                    # the signed release: `docker load` puts it here (DEPLOY section 2)
  image: deltasleuth:0.1.0             # always pyproject's version; source builds: docker-compose.source.yml
  pull_policy: never
  read_only: true                      # the app writes only to its volumes and /tmp
  tmpfs: ["/tmp:size=64m,mode=1777,noexec,nosuid,nodev"]
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
```

**The site file.** `deploy/docker-compose.site.example.yml` (new, tracked) is copied once to
`deploy/docker-compose.site.yml`. That copy is git-ignored, never in the kit, and so never overwritten
by an upgrade. It holds everything a site changes:
- secrets (the IdP credential, `llm_key`, target DSNs, the pack key);
- the PostgreSQL pin;
- limits;
- the subnet, with `!override` (Compose 2.24.4 or later).

```yaml
# This installation's changes to docker-compose.yml, which you never edit (DEPLOY section 3). Upgrades
# never overwrite this file. Compose adds these lists to the base file's. As copied it changes nothing:
# token sign-in with AI off needs no secret beyond the base file's four.
services:
  app:
    secrets: []                                # single sign-on: oidc_client_secret; AI chat: llm_key; each with its line under secrets: below
  # db: {image: "registry.bank.example/postgres@sha256:<64 hex>"}   # your verified PostgreSQL 16
# secrets:
#   oidc_client_secret: {file: ./secrets/oidc_client_secret}
#   llm_key: {file: ./secrets/llm_key}
# Another subnet (then auth.trusted_proxies names its gateway):
# networks: {default: {ipam: {config: !override [{subnet: 10.99.0.0/24, gateway: 10.99.0.1}]}}}
```

Since [FIRSTRUN.md](FIRSTRUN.md) §2 the example is a one-machine trial's as copied, like the settings
example: an office writes `secrets: [oidc_client_secret, llm_key]` and removes the `# ` of the three
lines under it, which gives the union this section was checked with.

**`.env`** (git-ignored, the site's) holds `COMPOSE_FILE=deploy/docker-compose.yml:deploy/docker-compose.site.yml`.
It gains `:deploy/docker-compose.ca.yml` with a private CA, or `:deploy/docker-compose.source.yml` for
a source build. Compose reads it from the working directory, so every command and the cron use the
site's files, with no `-f`. A `COMPOSE_PROJECT_NAME` line in it names the project of a second
installation on one machine, in place of the base file's `deltasleuth` (FIRSTRUN §4), and
`deploy/env.source.example` is a source build's `.env` as copied. The union of secrets, the `!override` subnet and `.env` were checked with
`docker compose config` on Compose 2.40.3. A command without the site file (`-f`, or an exported
`COMPOSE_FILE`) recreates a moved subnet's network on the base file's subnet, even `run --no-deps`, and
the running containers come back on it without their service names, so the app cannot resolve `db`.
`up -d --force-recreate` repairs it; a plain `up -d` recreates the network again, still without the
names (checked with a throwaway project on Compose 2.40.3). DEPLOY therefore runs every command through
`.env`.

**The source override,** `deploy/docker-compose.source.yml` (new; builds are unsigned):

```yaml
# An unsigned build of this checkout (DEPLOY section 2), tagged with the Compose project's name, so two
# checkouts on one machine share no image; after taking new source: docker compose up -d --build
x-image: &image "${COMPOSE_PROJECT_NAME:-deltasleuth}:source"
services:
  app: {build: {context: .., dockerfile: deploy/Dockerfile}, image: *image}
  mocks: {image: *image}                       # the app's build: one image, built once
```

It sets no `pull_policy`. With the inherited `never`, Compose builds only a missing image and never
pulls; `build` would rebuild at every `run` and `up` (both checked with `--dry-run`). Since FIRSTRUN §2
the tag carries the project's name, `deltasleuth:source` by default, and `mocks` runs the app's build
instead of building a second, untagged image. With the image absent, `docker compose --dry-run --profile
mocks up -d` builds `app` once and then creates both containers (Compose 2.40.3). On a daemon, on
2026-10-05, `up -d` with the image absent and `up -d --build` after a source change each built once
and ran both services on the one image id; the build a rebuild replaces stays, untagged (FIRSTRUN's
Checked block).

**The CA override,** `deploy/docker-compose.ca.yml` (new), sets `image: "deltasleuth:0.1.0-ca"` for
`app` and `mocks`. After each load, DEPLOY §11 builds it `FROM deltasleuth:<v>` (`USER root`,
`update-ca-certificates`, `USER 10001:10001`) with
`--label org.deltasleuth.base=<the id step 5 printed>`. The signed tag keeps its id, so steps 0 and 5
can be run again in the release's folder (step 5 reads the values step 0 sets).

**Checked** with local images under these flags, pulling nothing:
- `ds init` and `ds serve` answer `/v1/health` with 200;
- the mocks serve;
- `tempfile` works;
- DEPLOY §3's `-v ~/kek.from-vault:/tmp/kek:ro` mount works inside the tmpfs;
- `docker cp` works into a volume (and is refused on the root filesystem);
- root cannot enter the 0700 `p0` without `DAC_OVERRIDE`.

Re-checked on this Dockerfile with Docker 29.1.3 (Desktop, arm64, classic store) and Compose 2.40.3:
- the native build left the ids of both bases and `postgres:16-alpine`
  unchanged, so nothing was pulled; it runs as `10001:10001`, with no `uv`, `uvx` or `pip` on `PATH`
  and no `pip` module;
- tagged `deltasleuth:0.1.0.dev0`, it ran as a throwaway project, from a scratch folder
  laid out like the kit: `.env` named the base file and a site file that moved the port and, with
  `!override`, the subnet (another stack on the machine holds `127.0.0.1:8000` and `172.30.0.0/24`);
- each item above, plus DEPLOY §6's mock batch in tokens mode (35 MATCH, 2 MISMATCH, 2 KNOWN_DIFF,
  1 NOT_FOUND_B, `report.pdf` 200), §10's `pg_dump`, evidence `tar` and `ds audit verify`, the P0 cron
  without `-f` (it deletes), and §11's CA layer (its label is the base id, its layers begin with the
  base's, and Python trusts the CA);
- on Docker Desktop, `docker compose cp` kept the host file's uid and gid (501:20), not root's; the
  cron needs `DAC_OVERRIDE` either way.

No route parses multipart; bodies are read into memory.

**Consequences:**
- the P0 cron in DEPLOY §10 and RETENTION §9 gains `--cap-add DAC_OVERRIDE` after `-u root`, and loses
  `-f`: `cd /srv/deltasleuth && docker compose run …` uses `.env`. Every upgrade path in DEPLOY §10
  replaces an existing installation's earlier line, which fails every night under the hardening, and
  checks the new one once by hand with `-print`;
- volume files keep gid 999, which is harmless;
- resource limits are a site-file choice.

## 7. SBOM

**Every scanner run** has its working directory in staging and uses `-c <staging>/empty.yaml`. That
replaces every config file `grype config locations` lists: a hostile `.grype.yaml` in the working
directory and `~/.grype.yaml` (ignore rules, `only-notfixed`) and `~/.syft.yaml` (`exclude`) stopped
applying (checked). Its environment is built from scratch, because variables still apply under `-c`
(checked with `GRYPE_ONLY_NOTFIXED`). It holds:
- `PATH`: the resolved tools' folders, `/usr/bin` and `/bin`;
- `HOME=<staging>/home` and `TZ=UTC`;
- `GRYPE_DB_CACHE_DIR`: the maintainer's cache, `~/Library/Caches/grype/db`;
- `GRYPE_DB_AUTO_UPDATE=false`, `GRYPE_CHECK_FOR_APP_UPDATE=false` and
  `SYFT_CHECK_FOR_APP_UPDATE=false`;
- `HTTPS_PROXY` and `NO_PROXY`, if set, for the two network steps only (`grype db update`,
  pip-audit).

`build` refuses any `syft version -o json` or `grype version -o json` other than `release.SYFT`
(1.52.0) and `release.GRYPE` (0.119.0). Those constants change only on purpose.

```sh
syft scan docker-archive:image.tar -c empty.yaml -q --select-catalogers +javascript-lock-cataloger \
  --source-name deltasleuth --source-version <v> --source-supplier "<vendor>" \
  -o cyclonedx-json@1.6=bundle/sbom/<stem>.cdx.json -o spdx-json@2.3=bundle/sbom/<stem>.spdx.json
```

- **Input:** the uncompressed saved tar, the exact shipped bytes. Relative paths and UTC keep host
  paths and the timezone out, and without `--enrich` there is no network use. `--source-supplier`
  fills CycloneDX `metadata.supplier` (NTIA).
- **Covered:**
  - Debian packages and the distribution;
  - the CPython binary;
  - `/app/.venv`, including `deltasleuth`;
  - react, react-dom and scheduler, from the lock beside `web/dist`. Without that cataloger an image
    scan finds no npm package. Dev dependencies are excluded; the root `deltasleuth-web` appears
    unversioned.
- **Completeness (each a refusal):**
  - a CycloneDX `operating-system` component named `debian`;
  - the set of `pkg:deb/debian/` names, percent-decoded, equals the `ii` lines of
    `dpkg-query -W -f '${db:Status-Abbrev}${Package}\n'`, run in the built image with
    `--network none --entrypoint dpkg-query`;
  - `pkg:generic/python@<the image's python version>`;
  - every package in
    `uv export --frozen --no-dev --no-emit-project --no-hashes --no-header --no-annotate` as
    `pkg:pypi/<canonical name>@<version>`, once its marker holds for linux, CPython 3.13 and the
    target machine (`packaging.markers`; colorama, tzdata and httpx2-jsfetch drop out);
  - `deltasleuth@<v>`, and every non-`dev` entry of `web/package-lock.json`.

  On the current local image, syft run this way lists debian 13.7, the same 87 Debian packages as
  dpkg, and `pkg:generic/python@3.13.15`.

  Re-run on 2026-09-29 through the tool's own functions (`_sbom`, `lacks`, `_grype`, `_pip_audit`,
  `gate`), on §5's Dockerfile built natively (arm64) from the working tree, with the §7
  environment and `-c`:
  - syft 1.52.0: debian 13.7, 87 `pkg:deb` names, equal to dpkg's 87 `ii` lines; python 3.13.15;
    67 `pkg:pypi` (deltasleuth included); 4 `pkg:npm` (react and react-dom 19.3.0, scheduler 0.28.0,
    and the unversioned `deltasleuth-web`).
  - `uv export` gives 69 packages. The markers drop colorama, tzdata and httpx2-jsfetch for both
    x86_64 and aarch64, which leaves 66. `lacks` finds nothing missing for either.
  - 39 vendored files: pillow 12.3.0 has 18, psycopg-binary 3.3.6 has 16, scipy 1.18.1 has 3 and
    numpy 2.5.3 has 2.
  - The messages: `the SBOM lacks the Debian distribution`; `the SBOM lacks <package> (dpkg)`, and for
    the other direction of the equality, `the SBOM lists Debian package <package>, which dpkg does
    not`; `the SBOM lacks python 3.13.15`; `the SBOM lacks <name> <version> (uv.lock)`; `the SBOM
    lacks deltasleuth <v> (pyproject.toml)`; and `the SBOM lacks <name> <version>
    (web/package-lock.json)`.
  Since [ENGINES.md](ENGINES.md) (2026-10-02) the lock also holds `oracledb` 26.0.0 and `pymysql` 1.2.3,
  the drivers of MySQL and Oracle table sides: `uv export` gives 71 packages, so `lacks` requires both
  in the SBOM and pip-audit audits both, without a change to the tool. **No image was built with them
  and no scan was run:** the counts and the vendored files of this section are those of 2026-09-29. On
  the build machine's own platform (macOS arm64) `pymysql` is pure Python and `oracledb`'s wheel holds
  four compiled modules and vendors no library; whether its manylinux wheel adds a `*.libs/` file is
  not checked, and `build` refuses until each file it lists has a `[[vendored]]` decision (§8). The
  wheel also ships `thick_impl`, the code of Oracle's thick mode, which DeltaSleuth never loads
  (`oracledb.init_oracle_client` is called nowhere; a test holds that).
  Since [FILES.md](FILES.md) (2026-10-03) the lock also holds `duckdb` 1.5.5 (MIT; no
  dependency: pyarrow, pandas and numpy are extras and none is imported), the files reader: `lacks`
  requires it and pip-audit audits it, without a change to the tool. From `uv.lock` its cp313
  `manylinux_2_28` wheels are 21.5 MB (x86_64) and 19.4 MB (aarch64); installed on macOS arm64 it is
  45.9 MB, one compiled module, no `*.libs/` file. **Open before a release:** whether a manylinux wheel
  adds a `*.libs/` file is not checked (it needs the release image or a download), and `build` refuses
  until each file it lists has a `[[vendored]]` decision (§8). No image was built with it.

  **Rehearsed on 2026-10-04** (a scratch clone at 0.1.0rc1, a throwaway key, placeholder triage; nothing
  was delivered): `build --platform linux/amd64` ran for real, under emulation on the arm64 build machine.
  - syft 1.52.0 listed debian 13.7, 87 `pkg:deb` names, equal to dpkg's 87 `ii` lines, python 3.13.15,
    70 `pkg:pypi` (deltasleuth, `oracledb` 26.0.0, `pymysql` 1.2.3 and `duckdb` 1.5.5 among them) and the
    4 `pkg:npm`. `lacks` found nothing missing, the SPDX document holds 163 packages, and pip-audit
    audited the 69 others, skipping none.
  - Both open checks are closed, for linux/amd64 in that image and for linux/arm64 in an image built
    from source: the manylinux wheels of `oracledb` and `duckdb` hold no `*.libs` file and bundle no
    shared library. `oracledb`'s four modules link libc and libpthread alone. DuckDB's one module (60.4 MB
    on x86_64, 54.5 MB on aarch64) links glibc's libraries, `libstdc++.so.6` and `libgcc_s.so.1`, which
    Debian's `libstdc++6` and `libgcc-s1` supply and the SBOM lists.
  - The linux/amd64 image vendors 41 files, not arm64's 39: pillow 18, psycopg-binary 15, scipy 5 and
    numpy 3, under other names. Its psycopg-binary holds OpenSSL 3.5.8 alone (`libcrypto-2d1629ee.so.3`)
    and PCRE 8.32 (`libpcre-9513aab5.so.1.2.0`); OpenSSL 1.1.1k is in the arm64 wheel only, beside 3.5.8.
  - The image is 690 MB (690,185,282 bytes as Docker counts it), its gzipped save 211,334,005 bytes, and
    the bundle's transport tar 218,951,680 bytes; the build took 26 minutes, about 24 of them step 8's suite.
  - No linux/arm64 bundle was built: its base manifest is not in the local store (§9 step 3).

  **Since [MCP.md](MCP.md) (2026-10-05)** the lock also holds `mcp` 2.2.0, the MCP server's library,
  pinned exactly in `pyproject.toml` under the lock's `exclude-newer` (2026-09-21), and the three
  packages it needs that the lock did not hold: `mcp-types` 2.2.0, `opentelemetry-api` 1.44.0 and
  `sse-starlette` 3.4.11. `uv add` changed `uv.lock` by 75 added lines and moved no locked version.
  - **Licences,** read from the installed wheels' own metadata on the build machine (macOS arm64,
    2026-10-05), nothing downloaded: `mcp` and `mcp-types` say `License: MIT` and the classifier `License
    :: OSI Approved :: MIT License`; `opentelemetry-api` says `License-Expression: Apache-2.0`;
    `sse-starlette` says `License-Expression: BSD-3-Clause`. Each `dist-info` holds its licence text
    under `licenses/LICENSE` (MIT, MIT, Apache 2.0, the three-clause BSD text). Architecture Appendix C.2
    records them by hand, the licence gate not being built.
  - **Wheels.** Each of the four is one `py3-none-any` wheel in the lock (a test holds it), so the same
    bytes on amd64 and arm64: no compiled module and no `*.libs/` file (their `RECORD`s list none), so
    none needs a `[[vendored]]` decision (§8).
  - **A fifth entry.** The resolution also names `pywin32` 312 under `sys_platform == 'win32'`, which
    nothing installs on Linux or macOS and the image never holds.
  - **The SBOM gate.** `uv export` now gives 77 packages. The markers drop colorama, tzdata,
    httpx2-jsfetch and pywin32 for both x86_64 and aarch64, which leaves 73, the four new ones among
    them: `lacks` requires each as `pkg:pypi/<name>@<version>` and refuses with `the SBOM lacks mcp 2.2.0
    (uv.lock)` and its like, without a change to the tool (a test holds the four refusals, and that
    pywin32 is not asked for).
  - **The pip-audit gate.** pip-audit audits every `pkg:pypi` component of the SBOM but deltasleuth, so
    the four more, and `build` refuses a dependency it skips (§8), without a change to the tool.
  - **No telemetry backend.** The lock's only OpenTelemetry package is the API: no SDK, exporter or
    instrumentation (a test holds it). The MCP library opens a span per message through a middleware
    which the server drops (`server.build`), and the three entry-point groups the API's `OTEL_PYTHON_CONTEXT`,
    `OTEL_PYTHON_TRACER_PROVIDER` and `OTEL_PROPAGATORS` select from hold the API's own entries alone (a
    test holds it, in the development environment).
  - **Not run:** no image was built with these packages, and syft, grype and pip-audit were not run on
    them: the counts above this paragraph are those of the rehearsal of 2026-10-04, before the pin. By
    the lock, a build's SBOM lists 74 `pkg:pypi` and pip-audit audits 73 of them; neither is observed.
    PyPI's JSON listed no vulnerability for `mcp` and `mcp-types` 2.2.0 on 2026-10-04, read once for
    the contract; no pip-audit output was kept. `mcp` 2.3.0 (2026-10-02) is newer than the lock's date
    admits.
- **`vendored-libraries.txt`:** `python -c` in the built image walks each distribution's `RECORD` and
  prints `<distribution> <version>⇥<path>⇥<status> until <expires>` per `*.libs/*` file, with the
  decision from §8.
  - Today, on arm64, there are 39 files: psycopg-binary's libcrypto 1.1.1k and 3, libssl, libpq,
    krb5, ldap and sasl; pillow's image libraries; and numpy's and scipy's OpenBLAS and gfortran.
  - syft and grype see none of them; a site's binary scanners may. auditwheel puts a content hash in
    each name, so a changed library is a new name.
- **Not covered:** build-only tools, inlined Vite helpers, PostgreSQL, the host. And DuckDB's statically
  compiled third-party code ([FILES.md](FILES.md) §3: zstd, snappy, brotli, lz4, miniz, mbedTLS, RE2,
  ICU, jemalloc, Thrift, yyjson): syft, grype and the `*.libs` walk see none of it. **Open:** the wheel
  carries no notice of those libraries (its `dist-info` `LICENSE` is DuckDB's MIT text; its one other
  licence file, `duckdb/experimental/spark/LICENSE`, is Apache Spark's: both read in the rehearsal's image
  on 2026-10-04), so their versions and licences,
  which architecture Appendix C lists by hand, need DuckDB's source tree. **Observed**
  (2026-10-03), by the names in the installed macOS arm64 module (`strings` of
  `_duckdb.cpython-313-darwin.so`, nothing downloaded): ICU (namespace `icu_66`, data `icudt66l`), zstd,
  snappy, brotli, lz4, miniz, Mbed TLS, RE2, Apache Thrift, the Parquet format's generated code, yyjson,
  fmt (namespace `v6`), utf8proc, cpp-httplib, moodycamel's concurrentqueue and a skip list; no jemalloc
  name is in that module. The manylinux modules, inspected on 2026-10-04 in the rehearsal's images, hold
  the same names and jemalloc too (its version string, `5.3.0-196-ga25b9b8ba918…`), and a few version
  strings (architecture Appendix C.7). ICU's and fmt's major versions are all that the names give.
  `cpp-httplib` is an HTTP client compiled into the core, which is how `INSTALL` reached the network
  during the build with external access off (FILES §3, U3): no statement DeltaSleuth composes reaches it.

## 8. Scanning, gate, triage and VEX

- **grype:** `grype db update` runs once. Then `grype sbom:bundle/sbom/<stem>.cdx.json -c empty.yaml -o json`
  runs in the §7 environment. grype refuses a database older than 120 h. On today's image it finds
  159 matches from CycloneDX, SPDX or syft-json alike. Re-run on 2026-09-29, with database v6.1.9 built
  2026-09-29T06:32:31Z, it found 159 on both SBOMs:
  - High: 1 fixed, 2 not-fixed, 47 wont-fix;
  - Medium: 3 fixed, 49 wont-fix, 2 with no fix state;
  - Low: 1 fixed, 2 not-fixed, 8 wont-fix;
  - Negligible: 44 not-fixed.
- **pip-audit:** `sys.executable -m pip_audit -r pip-audit.txt --no-deps --disable-pip
  --progress-spinner off -f json -o bundle/scan/pip-audit.json`, in the §7 environment (no
  `PIP_AUDIT_*`). It audits `name==version` for each `pkg:pypi` component except `deltasleuth`, and
  adds PyPI/OSV (PYSEC) advisories. pip-audit exits 0 when it skips a package, so a dependency with a
  `skip_reason` is a refusal. On 2026-09-29, pip-audit 2.10.1 audited all 66 packages; it found no
  vulnerability and skipped none.
- **npm audit:** not a gate, because grype matches the same GHSA data for the three shipped packages.
  README keeps `npm audit --omit=dev` for developers.
- **The gate:** `release.gate(matches, audit, entries, today, vendored)`, a pure function. `vendored`
  holds the `(distribution, version, path)` of each vendored file.
  - A grype match with `vulnerability.fix.state == "fixed"` blocks if it is Critical, High or
    Unknown, or has a non-empty `knownExploited`.
  - A pip-audit vulnerability with `fix_versions` blocks; it carries no severity. A grype match with
    the same id and purl does not hide it: each source's finding is gated, and counted, on its own,
    and one entry triages both.
  - An entry in force (decided on or before today, not expired) lets a blocking finding pass when it
    names the finding's id, a `relatedVulnerabilities` id or an alias, and its `component` equals the
    artifact's purl without qualifiers.
  - Everything else is reported, not gated: not-fixed, wont-fix, unknown fix states, and Medium or
    lower (today, 47 Debian `wont-fix` Highs among them).
  - Every file in `vendored-libraries.txt` needs a `[[vendored]]` decision in force for its
    distribution and version. The scanners cannot see these libraries, so this is their triage.
- **The first release** needs a decision on CVE-2026-82049. grype matches it to CPython 3.13.15 as
  "fixed in 3.14.0b1". It also needs `[[vendored]]` decisions for the 39 files, OpenSSL 1.1.1k (EOL)
  first.
- **On 2026-10-04,** with database v6.1.10 built 2026-10-04T08:11:47Z, the rehearsal's linux/amd64 build
  was refused its 41 vendored files (§7), then 15 blocking findings among 209 matches:
  - CVE-2026-82049 on python 3.13.15, by now fixed in 3.13.16;
  - CVE-2026-54873, CVE-2026-84782, CVE-2026-84784 and CVE-2026-72897, each on Debian's `openssl`,
    `libssl3t64` and `openssl-provider-legacy` 3.5.7-1~deb13u2, fixed in 3.5.7-1~deb13u3;
  - CVE-2026-103111 on `libpcre2-8-0` 10.46-1~deb13u2, fixed in 10.46-1~deb13u3;
  - pip-audit's PYSEC-2026-4141 (GHSA-42vr-xj54-vc7v) on `pyjwt` 2.14.0, fixed in 2.15.0.

  A newer base pin and lock clear them, or the maintainer decides each. With a placeholder entry for each, all
  `affected` and marked as the rehearsal's, the gate read `passed; 0 blocking, 15 triaged, 195 reported,
  41 vendored file(s) decided`: High 14 fixed, 7 not-fixed and 48 wont-fix; Medium 22, 1 and 49; Low 13
  fixed and 10 wont-fix; Negligible 45 not-fixed.
- **Today's gate**, with the committed `tools/triage.toml`, which has no decisions yet (2026-09-29):
  `failed; 1 blocking, 0 triaged, 158 reported, 0 vendored file(s) decided`. The blocking finding is
  `CVE-2026-82049 High, fixed in 3.14.0b1: pkg:generic/python@3.13.15`, and all 39 vendored files are
  refused for want of a decision. For a hand check only, never written to the file, an in-memory
  `not_affected` decision on CVE-2026-82049 gave `passed; 0 blocking, 1 triaged, 158 reported`, and
  the VEX the tool wrote from it made grype move that match to `ignoredMatches` on both SBOMs. The
  decisions themselves are the maintainer's.

`tools/triage.toml` is read with `tomllib`. It does not ship, but its decisions ship as VEX and in
`SUMMARY.txt`:

```toml
# What the release gate accepts (docs/mvp/RELEASE.md §8), each decision with an expiry.
[[finding]]
id = "CVE-2026-82049"                     # grype's id, a related id, or a pip-audit id or alias
component = "pkg:generic/python@3.13.15"  # the SBOM purl without qualifiers: a new version needs a new decision
status = "not_affected"                   # or "affected": shipped with a dated action, never suppressed
justification = "vulnerable_code_not_in_execute_path"   # not_affected only: one of OpenVEX's five
statement = "One sentence a site's reviewer can check; for affected, the action and its date."
decided = 2026-10-01
expires = 2026-12-30                      # at most 90 days after decided (affected: 30)
by = "maintainer"                         # a role, never an e-mail address

[[vendored]]                              # a wheel's bundled libraries, which no scanner matches
distribution = "psycopg-binary"
version = "3.3.6"                         # another version needs a new decision
files = ["psycopg_binary.libs/libcrypto-02bae65c.so.1.1.1k"]   # as vendored-libraries.txt lists them
status = "affected"                       # or "not_affected", with a justification, as above
statement = "OpenSSL 1.1.1k is end of life; libpq uses it only for ...; moving to psycopg[c] by 2026-10-31."
decided = 2026-10-01
expires = 2026-10-31
by = "maintainer"
```

- **Refused entries:** a kind that is not a list of tables, an unknown key, a missing field or one of
  the wrong type, a bad status or justification, an over-long window, an expired entry, or one decided
  after today. So is a vendored file with no decision, or a decision for another version of a
  distribution the image ships when the image's version has none. A decision for another supported
  line's version stands while the image's version has its own, so one `rescan` covers both lines. An
  entry that matches nothing prints `remove it`.
  - Each malformed entry gets one line: `tools/triage.toml: <id> for <purl> <problem>`, or
    `[[vendored]] <distribution> <version> <problem>`. The problems are `has an unknown key k`,
    `lacks k`, `has status 's': use not_affected or affected`, `needs a justification if not_affected,
    and only then`, `has justification 'j': use one of OpenVEX's five`, `needs decided and expires as
    dates (YYYY-MM-DD)`, `needs k as a string`, `needs files as a list of strings`, `expires N days
    after decided: at most 90 (affected: 30)`, `expired on <date>: decide again`, `has decided <date>,
    in the future: date a decision the day it is made` and `names an e-mail address in by: name a
    role`. An unknown table gets `tools/triage.toml: unknown table t`, and a `[finding]` table (or any
    kind that is not `[[kind]]` entries) `tools/triage.toml: finding must be [[finding]] entries`.
  - A decision for another version gets `tools/triage.toml: [[vendored]] <distribution> <version> is
    not the image's <version>: decide again`.
  - `remove it` is a `warning:` line (`tools/triage.toml: <id> for <purl> matches no finding: remove
    it`), not a refusal.
  - An expired or future-dated entry is a refusal in `build`. `rescan` just does not apply it, so its
    finding blocks.
- **VEX:** `scan/vex.openvex.json` is OpenVEX 0.2.0, with `@id urn:deltasleuth:vex:<v>:<os>-<arch>`,
  author the vendor, and the signing time. Each matched `[[finding]]` becomes one statement:
  - `vulnerability {name: grype's id, aliases}`;
  - `products`: the component purl, plus `pkg:oci/deltasleuth@sha256%3A<manifest hex>?tag=<v>` (the
    purl spec's digest version, from `RELEASE.txt`) with it as a subcomponent;
  - `status`, with `justification` and `impact_statement`, or with `action_statement`.

  An OpenVEX statement needs a vulnerability, so `[[vendored]]` decisions go in `SUMMARY.txt` and
  `vendored-libraries.txt` instead, both signed.
- **Checked on grype 0.119.0:**
  - the purl, with or without qualifiers, suppresses on CycloneDX, SPDX, syft-json and daemon scans;
  - another version does not suppress, and neither does the oci product alone;
  - `affected` never suppresses;
  - the tool re-runs grype with `--vex` on both SBOMs, and refuses if a `not_affected` match remains.

  Other tools' use of the oci product, Trivy's included, is untested.
- **Weekly:** `release.py rescan <archived bundles>` applies today's database and triage, in the same
  environment. Exit 2 lists what would block and starts the SLA clock.
  - It prints `<stem>: <gate line>` per bundle, re-running grype on the bundle's CycloneDX and
    pip-audit on its PyPI packages.
  - A blocked bundle gets `error: <stem>: N finding(s) would block the release; upgrade, or triage each
    in tools/triage.toml:` and one line per finding.

## 9. `tools/release.py`

About 1,100 lines of Typer, modelled on dsv:
- every refusal is one `error:` line (`dsv.fail`) and exit 2;
- no subprocess gets a secret in argv or its environment;
- it never runs `docker pull`, `push` or `login`.

```text
uv run python tools/release.py keygen --out DIR [--passphrase file:/abs]
uv run python tools/release.py build --key file:/abs/release-<fp16>.key.pem --notes FILE --out DIR --register FILE
    [--platform linux/amd64|linux/arm64] [--passphrase file:/abs]
uv run python tools/release.py rescan BUNDLE...
uv run python tools/release.py sign --key file:/abs/… [--passphrase file:/abs] FILE      # writes FILE.sig
```

`sign` accepts only these:
- a text whose first line is `# DeltaSleuth security advisory DS-SA-YYYY-NNN`;
- a text whose first line is `# DeltaSleuth release key transition`;
- an OpenVEX JSON document.

Anything else, `SHA256SUMS` included, gets `FILE is not an advisory, a key transition or an OpenVEX
document: the release key signs nothing else`. Nor does `sign` accept any of them with a line that
`sha256sum -c` or `shasum -c` would check: blanks and a `\` allowed, then a digest of 40 hex digits or
more and a blank (`shasum -a 256 -c` takes SHA-1's 40 whatever `-a` says), or the tagged
`SHA… (name) = digest`. Both tools skip `#` lines, so such lines behind an advisory's header would pass
step 3 as a `SHA256SUMS`. It gets `FILE has a line that sha256sum or shasum would check: the release key
signs no checksum list, so begin no line with a digest or "SHA… ("`; a digest in an advisory follows a
label (`sums_sha256: <digest>`).

`build` stages in a hidden folder inside `--out` (`DIR/.deltasleuth-release-*`), beside the bundle's
folder, and renames it at the end. It makes `DIR`, never its parents. A refusal removes the staging
folder, and `DIR` too if `build` made it, so a refusal leaves nothing but, after step 9's register
append, the register line it names.

A refusal or failure after step 4's `docker build` also removes the image that build made:
`docker image rm deltasleuth:<v>`, never forced, and only while the tag still names the id recorded
after the build, so no other tag or image goes, and a retry finds no earlier attempt to leave untagged.
Once step 9 has moved the bundle into `DIR` the image stays, whatever becomes of the git tag.
- An image that held the tag before the build (another platform's image of the version, D2, or one
  loaded from a bundle) is not this run's. The build moves the tag off it, as `docker build -t` does. A
  refusal removes what it built and tags that image again (`docker tag <its id> deltasleuth:<v>`), so the
  store is as it was. A release leaves it untagged: `docker image ls --filter dangling=true` lists it.
- A build that gives the very id the tag named before it changed nothing in the store: a refusal then
  removes nothing.
- What could not be done follows the refusal as one `warning:` line: `deltasleuth:0.1.0 is still in the
  local image store (docker image rm: <docker's last line>)` where a container uses the image; `the image
  deltasleuth:0.1.0 named before the build is untagged now: run docker tag sha256:<64 hex>
  deltasleuth:0.1.0`; or, where the tag moved during the build, `deltasleuth:0.1.0 no longer names the
  image this build made, sha256:<12>…: nothing was removed`.

Only the docker stub has run the removal. The rehearsal's builds (§7) were made before it: each one
refused after step 4 left its image under `deltasleuth:<v>`, and the next attempt left that one
untagged. Remove any still in the store by hand: `docker image rm deltasleuth:<v>`, and an untagged one
by its id.

The steps of `build`:

1. **Key.** The prompt comes first, then `dsv.signer`'s refusals, then:
   - `--key: the key file is in a git working tree: keep release keys outside every repository`
   - `--key: the key file is in a cloud-synced folder (~/Documents): keep release keys on this machine only` (likewise `--passphrase`)
   - `--key: key <fp12>… is a licence vendor key: releases are signed with a release key (release keygen)`
   - `--key: key <fp12>… is not the signing key tools/release-keys.txt lists`
   - `release.PROCEDURE differs from the procedure filed with key <fp12>…: a changed procedure is filed like a new key (RELEASE.md §4)`

   Then the options, before anything runs:
   - `--notes: FILE is not a readable file`
   - `--notes: a release without vendor keys is an open build: FILE lacks the line "This release is an
     open build: it embeds no licence vendor key and enforces no licence." (LICENCE.md §19)`: a checkout
     whose `licence.SHIPPED_KEYS` is empty releases an open build, and its notes say so on a line of
     their own (`release.OPEN_BUILD`), which `NOTES.txt` and the tag's message carry. A vendor build's
     notes need no such line, and are refused with it:
   - `--notes: a release with vendor keys enforces a licence: FILE holds the line "This release is an
     open build: it embeds no licence vendor key and enforces no licence." (LICENCE.md §19)`: a checkout
     whose `licence.SHIPPED_KEYS` is not empty releases a vendor build, whose notes, copied from an
     open build's, would say that it enforces no licence.
   - `--register: FOLDER is not a writable folder`, and `--register: FILE is not a writable file`
   - `--out: PARENT is not a folder: build makes DIR, never its parents`
2. **Static checks,** the tools first, because the checks below run git and the scanners, then in
   this order:
   - `version 0.2.0.dev0 is not a release: set X.Y.Z or X.Y.ZrcN in pyproject.toml`
   - `the working tree is not clean: commit or remove 3 path(s) first` (untracked files count)
   - `tag v0.1.0 is on another revision: a released version is never rebuilt`. A tag on HEAD is
     another platform's bundle of this version (D2): `build` goes on, and step 9 leaves the tag as it
     is; the register still refuses a platform twice.
   - `deploy/docker-compose.yml names deltasleuth:0.0.9, not deltasleuth:0.1.0` (likewise the CA override)
   - `deploy/Dockerfile line 6: FROM node:24-alpine is not pinned by digest (name:tag@sha256:<64 hex>)`
     (an `ARG` default too). Keywords and stage names are case-insensitive, as Docker's, and a
     `COPY --from=` or `RUN --mount=…,from=` that names no earlier stage, by name or index, is a base
     too: `COPY --from=ghcr.io/astral-sh/uv:latest is not pinned…`, `RUN --mount from=… is not
     pinned…`.
   - `pyproject.toml names no vendor: set [project] authors = [{name = "<legal name>"}]`
   - `pyproject.toml [project].authors has an email: it would ship in the image and the SBOM; name the vendor only`
   - `SECURITY.md still holds <SECURITY-CONTACT>: name the security contact first`
   - `SECURITY.md still holds "Before the first release (the vendor's checklist)": delete that section first`
     (the heading, whatever became of the placeholder)
   - `grype 0.120.0 is not the approved 0.119.0: install it, or change release.GRYPE on purpose` (likewise syft)
   - `tools/triage.toml: CVE-… for pkg:… expired on 2026-12-28: decide again` (likewise `[[vendored]]`, a decision dated after today, and one line per malformed entry, §8)
   - `0.1.1 adds deltasleuth/store/migrations/011_….sql: a patch release adds no migration`
   - `0.1.0 for linux/amd64 is in the register already`
   - `--out: DIR/deltasleuth-0.1.0-linux-amd64 exists: a release is never overwritten`
   - `missing: syft (needs docker, git, grype, npm, syft and uv)`
3. **Base images.** These must be local, checked with `docker image inspect --format '{{.Os}}/{{.Architecture}}'`:
   - `python@<BASES[platform]>`, for the platform;
   - the node pin, and any other build base (a `COPY --from` or `RUN --mount` image), for the
     daemon's platform;
   - `postgres:16-alpine`.

   Their ids and the index reference's id are recorded.
   - `python@sha256:37134a49d21d… (python:3.13-slim for linux/amd64) is not in the local image store; this tool never pulls. To add it: docker pull python@sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332`
   - `postgres:16-alpine (the smoke test's database for linux/arm64) is linux/amd64 in the local image store, not linux/arm64` (likewise the node pin, `the build base for …`)

   Messages cut a digest to 12 hex digits. A local python image that carries only the index's
   RepoDigest resolves neither manifest digest in `BASES`: `build` stops here with the first message
   until the platform's manifest is pulled (§10 "Once").
4. **Build.** Extract the source archive as the context, then run
   `docker build --no-cache --provenance=false --platform P -f deploy/Dockerfile -t deltasleuth:<v>`
   with the build args `PYTHON`, `VERSION`, `REVISION`, `CREATED` (the commit time), `VENDOR` and
   `SOURCE_DATE_EPOCH`. Then check the result:
   - `the built image is linux/arm64, not linux/amd64`
   - `the built image runs as deltasleuth, not 10001:10001`
   - `the build changed the local image of python:3.13-slim@sha256:7c61056e61ac…: something pulled, and this tool never pulls`
5. **Smoke test** on an `--internal` network, with no secret:
   - `postgres:16-alpine` runs with `--pull never` and `POSTGRES_HOST_AUTH_METHOD=trust`;
   - the image runs with §6's flags, temporary volumes and a developer-mode config
     (`database_dsn_ref`, `evidence_dir`, `workspace_dir`, one dummy loopback target with
     `allow_private_network: true`, since `ds serve` loads the targets as it starts and refuses a
     loopback host without it);
   - `ds init`, then `ds serve --host 127.0.0.1`, polled through `docker exec`;
   - `ds schedules next '0 2 * * *' --tz Europe/London --count 1` under the same flags: schedules read the
     image's own time zone data ([SCHEDULES.md](SCHEDULES.md) §4 T5), which must hold the zone;
   - everything is removed afterwards, PostgreSQL's anonymous data volume included (`docker rm -f -v`).

   Messages: `the smoke test failed: /v1/health did not answer 200 within 180 s under the compose hardening`,
   which also covers every earlier failure, a failed `ds init` included; then
   `the smoke test failed: the image's time zone data does not load Europe/London`; then, for the file
   reader ([FILES.md](FILES.md) §3), `the smoke test failed: DuckDB does not load, its guard does not hold,
   or it cannot read and spill under the compose hardening` (`release.DUCK`: `duck.selftest()` under the
   same flags with the evidence volume, in which it spills; only the docker stub has run it).
   As amended by [READER.md](READER.md) §11: the self-test reads through the file reader's process,
   faults it and requires another to read, and on Linux requires the reader's data limit and its socket
   filter to hold; the message is `the smoke test failed: DuckDB does not load, its guard does not hold,
   it cannot read and spill, its process is not replaced when it ends, or its limits do not hold, under
   the compose hardening`. Where the built platform's machine (`release.MACHINES`) is not the build
   host's (`os.uname().machine`, `arm64` read as `aarch64`) the image runs under emulation and `_smoke`
   sends `duck.selftest(limits=False)`: a data limit would count the emulator (observed on 2026-10-04:
   a reader with one does not start under Rosetta, SIGTRAP) and the emulator loads no filter. The image
   and the compose files did not change. The release tool has still run this step on the docker stub
   only; the same command ran for real on 2026-10-04 in images built from the working tree
   (`tests/e2e/test_reader_image.py`, FILES §3): natively on linux/arm64 with both limits held, and
   without its limits in the amd64 image under emulation. On 2026-10-04 the rehearsal's
   `build --platform linux/amd64` (§7) ran `_smoke` for real, under emulation on an arm64 host: `ds init`,
   `ds serve` and `/v1/health`, the Europe/London cron and `duck.selftest(limits=False)`, which printed
   `file reader: duckdb 1.5.5 in a process of its own, replaced when it ended; limits not tried`; the
   containers, network and volumes were gone afterwards. By hand in that image, under the same flags,
   `duck.selftest()` ended with `Unstarted`, as the note above expects. Natively, and so with the
   reader's limits, `_smoke` has still not run. The config folder is mounted
   read-only from staging, and its DSN names the trusting PostgreSQL container.
   `test_the_smoke_config_loads` passes the config through the real settings and targets loaders. On
   2026-09-29 `_smoke` as it then was (before the schedules step, which only the docker stub has run)
   ran for real on a native throwaway build of this Dockerfile (arm64, Docker 29.1.3): it passed in 4 s,
   and the lists of containers, networks and volumes were the same afterwards.
   As amended by [MCP.md](MCP.md) §3 (2026-10-05): `_smoke`'s last command is `python -c "from
   deltasleuth.mcp import server; server.selftest()"` (`release.MCP`), under the same flags and mounts
   and `--rm`. It reads none of them: the self-test builds the MCP server without a store, serves every data class and the
   work tool to an operator, asks the MCP library's own client for the tools and the instructions in
   both protocol eras and prints `mcp: 11 tools listed in both protocol eras; no store was read`. A
   failure reads `the smoke test failed: the MCP server does not list its tools in both protocol eras
   under the compose hardening`. It shows that the four new wheels import and serve in the image, on its
   read-only root filesystem. **In the release tool only the docker stub has run this step:** no image
   was built with the library. The self-test itself runs in the test suite, on macOS arm64.
6. **Save and SBOM:** `docker save`, then the digests, syft, the `dpkg-query` and
   `vendored-libraries.txt` runs, then the gzip:
   - `the saved archive holds 2 images; expected deltasleuth:0.1.0 alone`
   - `the SBOM lacks psycopg-binary 3.3.6 (uv.lock)` (likewise the Debian distribution, a Debian package dpkg lists, or python 3.13.15)
   - `psycopg-binary 3.3.6 ships psycopg_binary.libs/libcrypto-02bae65c.so.1.1.1k with no [[vendored]] decision in tools/triage.toml`
7. **Scans and gate:**
   - `2 finding(s) block the release; upgrade, or triage each in tools/triage.toml:`, then one line per finding: `  CVE-… High, fixed in 1.2.4: pkg:pypi/x@1.2.3` (a known exploited one says `Medium, known exploited`; a pip-audit one says `(pip-audit)` for the severity it lacks)
   - `pip-audit could not audit 1 dependency: x 1.2.3 (<skip_reason>)`
   - `the VEX did not suppress CVE-… for pkg:…`
8. **Checks,** after the gate, because triage is the likelier iteration:
   - `uv run --frozen pytest -q -p no:cacheprovider` with `DS_REQUIRE_DB=1 DS_REQUIRE_TOOLS=1`
     (which covers LICENCE's `test_two_vendor_keys`: no vendor key, an open build's release, or two and
     more, a vendor build's; exactly one fails);
   - `uv run --frozen ruff check deltasleuth mocks tests examples tools`;
   - `npm --prefix web ci && npm --prefix web test`.

   Message: `checks failed: <command> (exit N)`, with `npm --prefix web ci` and `npm --prefix web test`
   as two commands, after the failed check's output. Then the tree must still be clean with HEAD at the
   revision: `the tree changed during the build (HEAD <12>…, 1 path changed): nothing was released`.
9. **Assemble:**
   1. Write `RELEASE.txt`, `VERIFY.txt`, `NOTES.txt`, `SECURITY.md`, any transition, and `SHA256SUMS`.
   2. Append the register line, then write the signature and the PEM.
   3. Self-check with `signing.verifies` and a re-hash.
   4. Write the transport tar in staging, move it and the folder into `DIR`, then, unless another
      platform's bundle made the tag, `git tag -a --cleanup=verbatim v<v> <revision> -F <notes>`
      (verbatim: git's default cleanup drops the notes' `#` lines). If the tag fails, the bundle
      stays: `the release is at DIR but git tag failed: run git tag -a --cleanup=verbatim v0.1.0 <revision> -F <notes>`.

   A failure after the append, the self-check's or an `OSError` (a full disk while the transport tar
   is written), removes the staging folder, a partial tar included, and gives one `error:` line that
   names the register line to strike:
   `the bundle failed its self-check: nothing was released; strike any line of sums_sha256 <64 hex> from REGISTER`,
   or `the bundle could not be written (<the OSError>): nothing was released; strike …`.
10. **Print:** `released DeltaSleuth 0.1.0 for linux/amd64, image sha256:<12>…, signed by key <fp12>…,
    tagged v0.1.0`, the sums digest, the transport sha256 and the gate line.

The tool has a few more refusals, each one `error:` line and exit 2:
- `--platform must be linux/amd64 or linux/arm64`;
- `SECURITY.md is missing: write it first (RELEASE.md §11)`;
- `deploy/Dockerfile has no ARG PYTHON: a release passes its platform's manifest (RELEASE.md §5)`;
- `tools/release-keys.txt line N is not "<fingerprint> <status> procedure=<sha256>"`;
- a tool that fails: `<tool> <subcommand> failed (exit N): <its last stderr line>`, and `pip-audit
  wrote no report`;
- `tools/transition.txt.sig is not key <fp12>…'s signature of tools/transition.txt` (§4);
- `the bundle failed its self-check: nothing was released; strike any line of sums_sha256 <64 hex> from
  REGISTER` (step 9);
- `--out: DIR/<stem>.tar exists: a release is never overwritten`, which also covers the transport tar;
- `keygen`: `DIR/procedure.txt exists: keygen never writes over a file`, and `DIR is in a git working
  tree: keep release keys outside every repository` or `DIR is in a cloud-synced folder (~/Documents):
  keep release keys on this machine only`;
- `sign`: `FILE.sig exists: sign never writes over a file`. `sign` checks FILE before it asks for the
  passphrase, and refuses a licence vendor key as `build` does, and a key the list marks retired or
  withdrawn: `--key: key <fp12>… is withdrawn 2026-10-01 in tools/release-keys.txt: it signs nothing
  more`.

## 10. Versions and the maintainer's checklist

- **Versions:** PEP 440 `X.Y.Z`, or `X.Y.ZrcN` for a candidate such as the pen-test build. The tag
  `v<version>` is annotated and local; pushing it is the maintainer's choice. One tag serves every
  platform's bundle of the version: the first `build` makes it, and the others build its revision. The
  first release is 0.1.0 (the version is now `0.1.0.dev0`).
- **Where the version lives:** pyproject (`system.start` records it), `uv.lock`'s `deltasleuth` entry
  (`uv lock` rewrites that one line), the compose tag and the CA override. A test keeps the four equal,
  `.dev` included; `web/package.json` stays unversioned. Rehearsed on 2026-10-04: with the lock's line
  left behind, `uv run` rewrites `uv.lock` as it starts the tool, which then refuses `the working tree
  is not clean: commit or remove 1 path(s) first`.
- **Once, before the first release.** Until every step is done, no release can be built: `build`
  refuses, in §9's order, at the first one still missing, with its §9 message. `build` refuses while
  SECURITY.md holds `<SECURITY-CONTACT>`, or the heading of a first-release checklist
  (`release.CHECKLIST`). From step 3 on the list is in §9's order, so a
  `build` run after any of those steps stops at the next one, never at a later one; step 1 comes
  first because §9 would miss it late, at its step 3, and step 2 changes no file: §9's step 1 asks for
  it once the key of steps 3 and 4 is listed. The rehearsal of 2026-10-04 saw that
  order: the signing key, the version, the vendor's name, `<SECURITY-CONTACT>`, the checklist's heading and
  the base, then, with the image built, the vendored files, the findings and the checks. In a clean
  checkout on the build machine, each step's change committed:
  1. **The amd64 base pull,** the maintainer's own command, since `build` never pulls
     (step 3 refuses without it):
     `docker pull python@sha256:37134a49d21d2120e4c4d73bb76f8a4ab9aef31f096f7ec2ead48c2feead4332`.
     An arm64 bundle needs `python@sha256:e2a5fce94bd761967528a12f16d707c2613e1522f3f2d77fa45766f45962547f`
     too. The pull adds a reference of its own and leaves `python:3.13-slim`'s local image alone (§5,
     by reasoning: the pull was not made here).
  2. **The open build's line in the notes** (LICENCE.md §19): the release embeds no licence vendor key,
     so none is generated, and the `--notes` file says, on a line of its own, `This release is an open
     build: it embeds no licence vendor key and enforces no licence.`; step 1 refuses without it. The
     vendor keys (LICENCE.md §4, Bootstrap: two `dsv keygen`, embedded and committed) are a step only
     before a vendor build's first release; a release version trusting exactly one fails step 8
     (`test_two_vendor_keys`), none and two or more pass, and step 1 refuses a vendor build's notes
     that still hold this line.
  3. **The release key:** `(umask 077; mkdir -p ~/deltasleuth-release-keys)`, then
     `uv run python tools/release.py keygen --out ~/deltasleuth-release-keys`, and the
     `tmutil addexclusion` it prints (§4, Custody). Without it step 1 refuses
     (`--key: key … is not the signing key tools/release-keys.txt lists`).
  4. **The key list:** keygen's line into `tools/release-keys.txt` and its row into SECURITY.md's key
     table (`test_release_keys_match_security_md`, `test_one_signing_release_key`); the fingerprint and
     `procedure.txt` go to each site out of band (§4,
     Publication).
  5. **The version:** `0.1.0` in pyproject, the compose tag and the CA override (`0.1.0-ca`), then
     `uv lock` for the lock's own line, the four files committed (step 2 refuses a `.dev` version before
     anything below, then a tree that is not clean and a compose tag that differs).
  6. **The vendor's name:** `[project] authors = [{name = "<legal name>"}]` in `pyproject.toml`, with no
     e-mail (step 2 refuses both).
  7. **The security contact:** a role address for `<SECURITY-CONTACT>` in SECURITY.md §2 (step 2
     refuses while the file holds the placeholder, or the checklist's heading).
  8. **The first triage** in `tools/triage.toml` (§8): CVE-2026-82049, and a `[[vendored]]` decision per
     vendored file. `build` lists both, and only now that steps 1 to 7 are done and committed. The amd64
     wheels' file names differ from §7's arm64 ones: the first amd64 `build` refuses each at step 6, and
     with those decided and committed the next one refuses the blocking findings at step 7. Each refused
     run removes its image (§9). Since [MCP.md](MCP.md) the image holds four more Python packages (§7:
     `mcp`, `mcp-types`, `opentelemetry-api`, `sse-starlette`): pure Python, so they add no vendored
     file to decide, but grype and pip-audit scan them for the first time in this run, and its smoke
     test is the first to list the MCP server's tools in a real image (§9 step 5).
  9. **The release:** with each of them decided and committed, `build` again: past the gate it runs step
     8's checks, signs and tags.
- **Each release:**
  1. Refresh the dependencies and triage. `mcp` is pinned exactly and its protocol moves between
     releases: after moving the pin, the tests of `tests/unit/mcp_server`, `tests/unit/api/test_mcp_http.py`
     and `tests/e2e/test_mcp_stdio.py` are what shows that the server still serves both protocol eras,
     that its HTTP route still reads the caller it was given, and that stdout still carries frames alone
     ([MCP.md](MCP.md) §14).
  2. Set the version (pyproject, the compose tag, the CA override, then `uv lock`) and commit.
  3. Run `build`, and for another platform `build --platform linux/arm64` on the same revision. While
     the checkout embeds no vendor key, the notes carry the open build's line, and never once it
     embeds one (§9 step 1).
  4. Verify the bundle with LibreSSL elsewhere.
  5. Deliver it, recording the site, version, date and `sums_sha256`.
  6. Archive it.
  7. Bump to `.dev0`, in the same four files.
- **Weekly:** `rescan` of every supported line's bundles, from one checkout (§8).

## 11. `SECURITY.md` (repository root; ships in every bundle and at its root)

1. **Supported versions:** the latest minor, and the previous one for 6 months after its successor.
2. **Reporting** to `<SECURITY-CONTACT>`, a role address the maintainer fills in (never a personal one;
   `build` refuses the placeholder).
   - Include the version, `config` and the steps.
   - Never send production or personal data.
   - An encrypted channel is available on request, and good-faith research has safe harbour.
3. **Targets** (proposed). Severity is the CVSS v4.0 base score (v3.1 too), adjusted for the
   documented deployment with written reasons. The clock starts at confirmation, or for a dependency
   once a fix exists and DeltaSleuth is affected. A dependency finding that does not affect
   DeltaSleuth gets `not_affected` in the next release, or a signed interim VEX within 10 business days of a request.

   | Severity | Acknowledge | Assess | Fixed release or mitigation | Advisory |
   |---|---|---|---|---|
   | Critical | 2 business days | 5 business days | 14 days; guidance within 72 h | at the fix; within 24 h of confirmed exploitation |
   | High | 2 business days | 5 business days | 30 days | at the fix |
   | Medium | 2 business days | 10 business days | 90 days | release notes |
   | Low | 5 business days | — | next minor, ≤ 180 days | release notes |

4. **Disclosure:** coordinated: sites that run a release are told first.
   - The embargo lasts until the fix plus 14 days, and at most 90 days.
   - Credit is given unless declined.
   - CVEs are requested through a CNA for our own Critical and High findings.
5. **Advisories:** `DS-SA-YYYY-NNN.txt`.
   - Contents: summary, affected versions and `config`s, severity and vector, CVEs, the fixed version
     and its `sums_sha256`, mitigations, VEX and credit.
   - Signed with `sign`, and checked with steps 1–2. A site accepts an advisory, or a signed interim
     VEX, only from a key its register lists as `signing` or `standby` on the day it arrives, never a
     retired or withdrawn one.
   - Sent to the named security contacts; Critical ones also by phone.
   - The compromise advisory is the exception: never signed with the stolen
     key, and confirmed by a phone read-back of its sha256.
6. **Fixes, air-gapped:** always a complete signed bundle, never a delta, verified on the Docker host
   and installed per DEPLOY, with no remote access.
7. **Verifying a release:** the key table (fingerprint, status, `procedure=`), "What the signature
   proves", rotation in §4's order (the new key `standby` in the old key's last bundle) and
   compromise. A bundle signed by a withdrawn key is genuine only if its list digest appears in that
   key's compromise advisory.
8. **Scanning our releases:** the weekly re-scan of every supported release (§10), where a finding
   that would block a release starts item 3's clock. Air-gapped: `grype db import` the archive named
   by `grype db status`'s `from`, or a newer one from `grype db list`, fetched elsewhere. Then run
   `GRYPE_DB_AUTO_UPDATE=false GRYPE_CHECK_FOR_APP_UPDATE=false GRYPE_DB_VALIDATE_AGE=false grype sbom:sbom/<stem>.cdx.json --vex scan/vex.openvex.json`.
   - Other scanners, Trivy and Dependency-Track among them, read the SBOMs. Whether Trivy applies our
     VEX is untested, and Dependency-Track cannot import OpenVEX.
   - Expect Debian wont-fix findings, the vendored libraries in binary scanners (see
     `sbom/vendored-libraries.txt` for our decisions), and newer findings, which should be reported.
9. **Pen testing:** the scope an independent firm tests DeltaSleuth against (PENTEST.md).
10. **By design, not vulnerabilities:** the docker group is root-equivalent; an open build enforces no
    licence, and a vendor build's licence is a contractual control; developer mode is loopback-only.
11. **Scope:** the image, the kit, the docs and the advisories. Not a site's infrastructure,
    PostgreSQL, the IdP or LLM providers.

The maintainer keeps a private vulnerability register: id, source, component, severity, dates, versions and
VEX status.

`build` refuses while SECURITY.md holds the heading "Before the first release (the vendor's checklist)"
(`release.CHECKLIST`), whatever became of the placeholder, so no bundle ships such a section.
§2 holds the one `<SECURITY-CONTACT>`. The key table's rows use keygen's format, `| <fp> | <status> | <sha256> |`; it has none yet.

## 12. `docs/mvp/PENTEST.md` (the vendor's scope): outline

1. **Target:** a named signed release or rc, installed from its bundle, verification included. The
   report names the version and `config`.
2. **Surface:**
   - `test_roles.ROUTES` × the six roles, as a matrix, plus the public routes;
   - OIDC, `__Host-` sessions, and E1's plain cookies on loopback http, CSRF, reauth, token ceilings;
   - four-eyes bypass;
   - YAML, CSV and PDF injection;
   - JSONata and CEL sandboxing and exhaustion;
   - SSRF and rebinding;
   - hostile responses and chat prompt injection;
   - masking and 410;
   - the audit chain;
   - pack, licence and release forgery;
   - the container (CIS Docker Benchmark).
3. **Lab:**
   - an x86_64 VM (4 vCPU, 16 GB), Docker 24+, nginx with a lab CA;
   - the mocks, `examples/tables` and a target the tester controls;
   - a pack key (and, for a vendor build, an Enterprise lab licence: an open build needs none);
   - data seeded per RUNBOOK "Try it on the mock pairs";
   - a snapshot;
   - a hosted IdP tenant (Keycloak only after an image pull);
   - chat only under an eval profile.
4. **Accounts:** one per role, two approvers and a requester, an unmapped user, a disabled user, and
   tokens (one expired: made at least 24 hours before the window, and `expired` in `ds token list`).
   Credentials go out of band.
5. **Materials:** grey box: the source archive, docs/mvp, and, sent apart because the kit leaves them
   out, architecture §13 and Appendix D and the release tag's `tests`, which hold the tests items 2
   and 9 name. Checked against ASVS 5.0 L2, the API and LLM Top 10s, and CIS Docker.
6. **Rules of engagement:**
   - windows, IPs and contacts;
   - no volumetric DoS, social engineering, physical tests or third parties;
   - Critical findings to `<SECURITY-CONTACT>` within 24 h (as built: to the contact SECURITY.md §2
     names, so that the placeholder lives in the one file `build` checks, and no kit ships it unfilled);
   - a TLP:AMBER, encrypted report;
   - data destroyed afterwards;
   - a retest within 30 days.
7. **Out of scope:** the site's hosts, proxy, IdP and PostgreSQL; the build machine and `tools/`;
   licence bypass by administrators (a vendor build's licence); docker-group users; SOC 2.
8. **Known limitations:**
   - the app connects as the database owner;
   - no TLS in `ds serve`;
   - P0 files are in clear;
   - the CLI's operator is asserted, not verified;
   - chat copies stay at the LLM provider;
   - OpenSSL 1.1.1k is vendored (its `[[vendored]]` decision). As built: in the linux/arm64 wheel only;
     the linux/amd64 image, the lab's, vendors OpenSSL 3.5.8 and PCRE 8.32 (§7).
9. **Existing coverage:** `tests/unit/api`, `auth`, `connectors/test_guards`, and the tamper tests.
10. **Findings flow:** a register id and the vendor's CVSS, the SLAs, a fix in an rc, a retest. Then
    the attestation and the remediation list, and an advisory if a shipped version is affected.

## 13. Tests

| File | Tests |
|---|---|
| `tests/unit/test_release_tool.py` (CliRunner; `release.ROOT` a temporary git repository; stub `docker`, `syft`, `grype` and `uv` on `PATH` following a scenario and logging argv, cwd and environment; real git and openssl) | `test_keygen_writes_an_encrypted_key_and_the_procedure` (and its hash on the printed line); `test_keygen_refuses_a_git_working_tree_or_a_synced_folder`; `test_build_writes_the_bundle` (files, `SECURITY.md` at the root, sorted sums, the signature verifies, digests match the archive, gzip mtime 0, only `SHIPPED` in the source and no site file, both VEX products with the digest purl, the register before the signature, the tag on the revision after the rename); `test_every_refusal_leaves_nothing` (every §9 message, the e-mail, scanner versions, a vendored file without a decision, a pip-audit skip, the procedure hash, a changed base id and a commit made during the build among them); `test_no_pull_push_login_or_secret_in_argv`; `test_scanners_run_isolated` (with a hostile `$HOME/.grype.yaml`, a `.grype.yaml` in the cwd and `GRYPE_ONLY_NOTFIXED=true` set: each scanner gets `-c <staging>/empty.yaml`, the staging cwd and only §7's variables); `test_no_host_path_in_the_bundle`; `test_packaging_is_deterministic`; `test_gate` (vendored decisions included); `test_sbom_completeness` (Debian, dpkg, python, PyPI, npm); `test_rescan_applies_todays_triage`; `test_sign_accepts_advisories_and_refuses_sums`; `test_real_syft_and_grype`, under the same hostile setup, skipped without the tools or a database |
| `tests/unit/test_release_tamper.py` (`tools()`, `steps()` and `run_step()` from `tests/unit/reports/packs.py`; sha256sum, shasum, LibreSSL; a docker stub) | `test_the_untouched_bundle_passes_its_own_procedure`; `test_each_tamper_fails_its_step` (the §3 list, the consistent swap after a pass elsewhere included); `test_deploy_quotes_the_procedure` |
| `tests/unit/deploy/test_config_examples.py` | `test_dockerfile_pins_every_base_by_digest` (every `FROM`, directly or through a pinned `ARG` default; `release.INDEX` is the `PYTHON` default's digest; `release.BASES` holds one manifest digest per platform); `test_runtime_stage_holds_no_installer`; `test_uv_requirements_reach_the_build`; `test_compose_runs_the_release_hardened`; `test_site_example_restores_todays_secrets` (base plus site example give today's `app.secrets`); `test_deploy_edits_only_the_site_file` (DEPLOY names a tracked compose file only in `COMPOSE_FILE` values, the "never edit" rule and the move's `git diff`); `test_root_p0_cron_adds_dac_override_and_no_file_flag`; changed: `test_deploy_writes_backups_outside_the_checkout` skips the block equal to `release.PROCEDURE` and allows `.env` |
| `tests/unit/test_release.py` | `test_release_keys_match_security_md`; `test_one_signing_release_key`; `test_the_signing_key_files_this_procedure`; `test_release_keys_are_not_vendor_keys`; `test_triage_is_well_formed` |

As built:
- `tests/unit/releases.py` holds the scratch checkout, the release key and the stubs that both tool
  test files use. A stub's log leaves out `__CF_USER_TEXT_ENCODING` and `LC_CTYPE`, which macOS's
  CoreFoundation and PEP 538 add to the stub interpreter's own environment.
- Beside the table's tests: `test_a_failed_tag_keeps_the_signed_release`,
  `test_triage_refuses_malformed_entries` and `test_the_old_keys_last_bundle_ships_its_transition`.
- Also, in the tool's file: `test_a_failure_after_the_register_names_its_line` (a
  failed self-check, a full disk), `test_one_version_for_each_platform`, `test_the_smoke_config_loads`,
  `test_pins_reads_every_base` and `test_rescan_passes_two_lines_that_vendor_other_versions`;
  `test_step_5_runs_again_only_after_step_0` (tamper); `test_every_compose_file_value_keeps_the_site_file`
  (deploy); `test_the_maintainer_checklist_is_the_heading_build_refuses` and
  `test_only_section_2_holds_the_security_contact_placeholder` (`test_release.py`). Changed:
  `test_sign_accepts_advisories_and_refuses_sums` (checksum lines behind a header, a withdrawn key),
  `test_the_old_keys_last_bundle_ships_its_transition` (the new key `standby`, then the old one
  `retired`, which `sign` refuses), `test_dockerfile_pins_every_base_by_digest` (case-insensitive;
  every `COPY --from` names a stage), `test_root_p0_cron_adds_dac_override_and_no_file_flag` (the
  move's hand check with `-print`), `test_packaging_is_deterministic` (no tag deletion),
  `test_every_refusal_leaves_nothing` (the new refusals), and the docker stub, which models
  PostgreSQL's anonymous volume.
- `test_release_keys_match_security_md` and `test_deploy_quotes_the_procedure` skip without SECURITY.md
  (§11) or DEPLOY's "Install a signed release" (§14). Both now exist, so both run: the key table
  equals the key list (both empty until §10's steps), and DEPLOY quotes the procedure verbatim.
- `test_real_syft_and_grype` saves the local `postgres:16-alpine`, the image the database tests
  already need. Under `DS_REQUIRE_TOOLS=1` it fails instead of skipping.
- For schedules ([SCHEDULES.md](SCHEDULES.md) §14): `test_no_pull_push_login_or_secret_in_argv` asserts
  the smoke test's `ds schedules next '0 2 * * *' --tz Europe/London --count 1` in the built image (its
  argv, `--rm` and `--read-only`), and `test_every_refusal_leaves_nothing` gains `the smoke test failed:
  the image's time zone data does not load Europe/London`, the docker stub answering 2 for that command.
  Only the stub has run this step: no real image has (§9 step 5).
- For files ([FILES.md](FILES.md) §3): the smoke test's last command is `python -c "from
  deltasleuth.connectors.tables import duck; duck.selftest()"` (`release.DUCK`) under the hardening with
  the evidence volume, which it spills in; `test_no_pull_push_login_or_secret_in_argv` asserts its argv,
  `--read-only` and the volume, and `test_every_refusal_leaves_nothing` gains `the smoke test failed:
  DuckDB does not load, its guard does not hold, or it cannot read and spill under the compose
  hardening`, the stub answering 1. Only the stub has run it in the release tool; the self-test itself
  runs in `test_duck_guard.py`, on macOS arm64. **U4's figure** (FILES.md §3): 200,000 rows of 132 bytes
  spill instead of failing from a `memory_limit` of 72 MiB with 2 threads (70 MiB fails; from 90 MiB they
  fit in memory; DuckDB 1.5.5 on macOS arm64), so the self-test sorts them at 80 MiB; on the release
  image's platforms the figure is not observed until a real smoke test runs.
- For the MCP server ([MCP.md](MCP.md) §3, §16): `test_no_pull_push_login_or_secret_in_argv` asserts the
  smoke test's last command, `python -c "from deltasleuth.mcp import server; server.selftest()"`, with
  `--rm` and `--read-only`; `test_every_refusal_leaves_nothing` gains `the smoke test failed: the MCP
  server does not list its tools in both protocol eras under the compose hardening`, the docker stub
  answering 1; `test_the_mcp_library_is_pinned_and_brings_no_telemetry_backend` holds the exact pin, the
  four packages at their versions as one `py3-none-any` wheel each, `lacks` requiring the four and not
  `pywin32`, a lock whose only OpenTelemetry package is the API, and the three entry-point groups
  holding the API's own entries alone. Only the stub has run the smoke step; the self-test itself runs
  in `tests/unit/mcp_server/test_tools.py`.
- For the reader's process ([READER.md](READER.md) §11, §15): `test_every_refusal_leaves_nothing` holds
  the smoke step's new message; `test_the_self_test_tries_its_limits_natively_alone` asserts, for six
  pairs of build host and platform, that `_smoke` sends `duck.selftest()` where the platform's machine is
  the host's and `duck.selftest(limits=False)` where it is not (`arm64` and `aarch64` one machine); the
  docker stub answers either text. `tests/e2e/test_reader_image.py` (skipped unless `DS_IMAGE` names an
  image; nothing pulled, each container with no network) runs the self-test and READER §3's other image
  tests under `release.HARDENED`. On 2026-10-04 it passed in an image built from the working tree
  natively on linux/arm64 (8 tests: the self-test's sort at 80 MiB spilled there too), and in one built
  for linux/amd64 under emulation (7 tests, the default data limit's skipped). The module now
  holds 9 tests, the ninth that a `ds` command leaves no reader behind (READER §3, U34), and all
  9 passed in the linux/arm64 image built again from the working tree; the amd64 image was not built
  again, so its 7 are of the earlier tree. No release was built.
- After the rehearsal of 2026-10-04, which built a bundle of a scratch clone and no release: the tool's
  file gains `test_an_affected_finding_ships_with_its_action` (an `affected` `[[finding]]` has no
  justification, and `_summary` failed on it with a `KeyError` after the gate);
  `test_the_version_is_one_in_pyproject_compose_and_the_ca_override` also holds `uv.lock`'s `deltasleuth`
  entry (§10); and two tests that held only before a release hold in one: `test_no_key_I1`
  (`test_licence.py`: an empty `VENDOR_KEYS`, no longer `SHIPPED_KEYS`, which a vendor build's bootstrap fills) and
  `test_the_server_start_records_the_role_mapping` (`api/test_audit.py`: pyproject's version, no longer
  `0.1.0.dev0`). With them step 8's suite passed in the clone at 0.1.0rc1.
- For the first-run fixes ([FIRSTRUN.md](FIRSTRUN.md) §12), in `tests/unit/deploy/`:
  `test_config_examples.py` holds `test_the_examples_start_a_trial_as_copied` (in place of
  `test_config_examples_load_and_serve_the_mock_specs`), `test_one_rule_switches_every_setting_on`,
  `test_the_office_lines_load_when_uncommented`,
  `test_site_example_adds_no_secret_until_its_lines_are_on` (in place of
  `test_site_example_restores_todays_secrets`: as copied the example changes nothing, and after its
  documented edit base plus site give that test's `app.secrets`),
  `test_the_source_override_builds_once_and_tags_by_project`, `test_the_env_example_is_the_source_builds`,
  `test_the_examples_render_in_compose_as_copied` and `test_the_developer_example_runs_from_a_checkout`;
  `test_every_compose_file_value_keeps_the_site_file` also reads `deploy/env.source.example`. The new
  `test_up_script.py` runs `deploy/up.sh` in a scratch checkout under `sh`, and `dash` where installed,
  against stand-ins for `docker`, `curl`, `sleep` and `uname` that follow a scenario and log argv and
  environment, with the real `openssl`. **The checks need `shellcheck`** on the build machine, as they
  need `openssl`: `test_up_sh_passes_shellcheck` skips without it and fails under `DS_REQUIRE_TOOLS=1`,
  which §9's step 8 sets, and so does `test_the_examples_render_in_compose_as_copied` without `docker`
  (it renders with `docker compose config`, which asks no daemon). `release.TOOLS` is unchanged: the
  tool itself runs neither. With the user guide (`docs/guide`, in `SHIPPED` since FIRSTRUN §10): the
  scratch checkout holds a chapter, `test_build_writes_the_bundle` compares the archive's top folders
  with `SHIPPED`'s and holds that the chapter shipped, and `tests/unit/deploy/test_guide.py` holds the
  guide's links to paths under `SHIPPED`, its quotes of `deploy/up.sh` to the script, and a step that
  needs `tests` or `tools`, which no kit holds, to saying so;
  `test_every_compose_file_value_keeps_the_site_file` reads the guide's chapters too.
- For the image a refused build removes (§9), added after that rehearsal: the docker stub answers
  `image rm` of one reference and `tag`, and keeps, untagged, the image a build moves the tag off.
  `test_every_refusal_leaves_nothing` also holds that no image is left; the tool's file gains
  `test_a_build_refused_at_the_gate_leaves_no_image` (one unforced `image rm` of the tag per attempt),
  `test_a_released_build_keeps_its_image`, `test_a_refused_build_gives_the_tag_back`,
  `test_a_refused_build_removes_only_the_image_it_built` (a tag moved during the build) and
  `test_a_refused_build_never_forces_its_image_out`; `test_a_failed_tag_keeps_the_signed_release` holds
  the image as well, and `test_a_failure_after_the_register_names_its_line` its removal.

## 14. Documents this contract amends

- **New:** `SECURITY.md`, `docs/mvp/PENTEST.md`, this contract as `docs/mvp/RELEASE.md`, the three
  `tools/` files and `tools/release-procedure.txt` (§5), `deploy/uv-requirements.txt`, the site example
  and the two overrides.
  `.gitignore` gains `/.env` and `deploy/docker-compose.site.yml`.
- **DEPLOY:**
  - The intro: run from `/srv/deltasleuth`. Once: copy the site example, then write `.env`
    (replacing `export COMPOSE_FILE`). "Changes take effect" names the site file, and says never to
    edit the tracked compose files. Every compose command runs through `.env`, never with `-f` or an
    exported `COMPOSE_FILE`: one without the site file drops its secrets and, with a moved subnet,
    recreates the network and cuts the app off from `db`, which `up -d --force-recreate` repairs.
  - §1: Docker 24+ with Compose v2 (2.24.4 or later to move the subnet); OpenSSL or LibreSSL;
    sha256sum; no build-time access. PostgreSQL 16 comes from the site's mirror or a verified load,
    pinned in the site file.
  - §2 becomes "Install a signed release", as a non-root user on the Docker host:
    1. Check the transport tar: its sha256 against the ticket; `tar -tvf` lists only files and
       folders, and `tar -tf` only `<stem>/`.
    2. Extract it with `--no-same-owner --no-same-permissions`.
    3. Only then copy the register's procedure beside it as `FILED-RELEASE-PROCEDURE.txt`.
    4. Run the whole procedure there, typed from that copy (quoted verbatim).
    5. `tar -xzf source/… -C /srv/deltasleuth --strip-components=1 --no-same-owner`.

    The old §2 becomes "Build from source (unsigned)": `.env` adds the source override, and
    `docker compose up -d --build` follows new source.
  - §3, §7 and §8: every compose edit goes to the site file: the tokens-mode and AI-off deletions,
    `oidc_client_key`, the steps that add a secret, target DSNs and the pack key. §5: the subnet
    moves to the site file, with the warning above beside it.
  - §5, acceptance item 21: `docker inspect --format '{{.Image}}' deltasleuth-app-1` is `config` or
    `manifest`. With a private CA it is the `-ca` image's id; that image's `org.deltasleuth.base`
    label is `config` or `manifest`, and its `RootFS.Layers` begin with those of `deltasleuth:<v>`
    (one `grep -cF` prints 1).
  - §10:
    - the Upgrade row, which ends every upgrade with the P0 cron's new line;
    - "Upgrading from a release bundle": verify on the host, back up (the rollback point), extract
      the kit, rebuild `-ca`, `up -d`, `ds audit verify`, check the P0 cron;
    - rollback to the previous bundle's kit and image, restoring only across a migration;
    - "Moving from a source checkout": move each change of `git diff deploy/docker-compose.yml`
      into the site file, write `.env` (with any other file of an exported `COMPOSE_FILE` after the
      site file), drop that export, upgrade, then replace the P0 cron and check it once by hand with
      `-print`;
    - "Re-scanning a release", with SECURITY.md's weekly re-scan;
    - step 2 of both "Upgrading to encryption at rest" and "Upgrading from the shared API token" (the
      new kit and image; the IdP secret in the site file);
    - step 1 of "Upgrading to licence keys";
    - the P0 cron (no `-f`, `--cap-add DAC_OVERRIDE`).
  - §11:
    - TLS through `-ca` with the label;
    - the rows "not mounted" (add it to the site file) and "UI shows 404" (a release: steps 0 and 5,
      run again in its folder; a source build: `docker compose build --no-cache app`);
    - troubleshooting for "image not found", failing steps, read-only errors and Podman's unprefixed
      `.Id`; "Step 5 prints 0 under Docker" first runs step 0 in the same shell, before any
      `docker image rm`.
- **RETENTION** §9 (the cron, and replacing an earlier one).
- **examples/tables:** its README and `compose.yaml` name the demo's file in `.env` after the site
  file, never in an exported `COMPOSE_FILE` (checked with `docker compose config`: the site file's
  secrets and subnet stay, with or without the CA or source override).
- **LICENCE** §15 step 1: the release route.
- **RUNBOOK:** "Setup", "Security and privacy", "Troubleshooting".
- **README:** Quickstart and Layout.
- **CONTRACTS:** §1, §9, §10.
- **PLAN.**
- **architecture:** N5 points to PENTEST.md; §13.1 "As built"; D.1's supply-chain row; C.7, where the
  licence gate is not built.

As built:
- DEPLOY keeps its section numbers, which the compose files and the other documents cite: "Build from
  source (unsigned)" is the last subsection of §2, "Install a signed release". Its step 1 checks the
  tar with `grep -v '^[-d]'` on `tar -tvf` and `grep -v '^<stem>/'` on `tar -tf` (each prints nothing;
  checked with bsdtar 3.5.3 and GNU tar 1.35 on a `release._transport` tar, and on a hostile one with a
  symlink and an entry outside the folder, which both print). Step 3 copies the filed procedure to
  `~/releases/FILED-RELEASE-PROCEDURE.txt`, beside the folder. Step 5 creates `/srv/deltasleuth` on the
  first install (`sudo install -d -o "$(id -un)"`).
- §5's acceptance item 21 is four commands, run in the release's folder; with a private CA the label
  and `grep -cF` checks were run on a throwaway image built as §11 builds the CA layer.
- §10's "Moving from a source checkout" takes either route: a release's kit extracted over the
  checkout, or new source with the source override; "Upgrading to encryption at rest" and "Upgrading
  from the shared API token" point their step 2 at its steps 1 to 4, and their start at its step 6
  (the P0 cron).
- §11 gains "Installing a release", one row per unexpected output of §2, the Podman row among them
  (`sed -i 's/^/sha256:/' ../id.txt`, then step 5's last command; untested, as Podman is not installed
  here), and rows for "Read-only file system" and the P0 cron's "Permission denied".
- RUNBOOK's "Troubleshooting" gains the read-only, "No such image" and verification rows; architecture
  C.7 states that the licence gate and deny-list are not built.
- [ENGINES.md](ENGINES.md) (2026-10-02) adds two runtime dependencies, `oracledb` (thin mode only) and
  `pymysql`, pinned exactly under the lock's `exclude-newer`; §7 says what was and was not checked of
  them, and architecture Appendix C.2 records both licences by hand, the licence gate not being built.
  DEPLOY, RUNBOOK, PENTEST and `SECURITY.md` §10 and §11 describe the engine targets.
- [FILES.md](FILES.md) (2026-10-03) adds `duckdb`, pinned exactly under the lock's
  `exclude-newer`; §5's Dockerfile creates `/var/lib/deltasleuth/files`, the mounts' parent, and
  `/var/lib/deltasleuth/spill`, the mount point of a spill volume of its own (DEPLOY section
  10), and §7, §13 say what was and was not checked of it and of its smoke step.
- [MCP.md](MCP.md) (2026-10-05) adds `mcp`, pinned exactly under the lock's `exclude-newer`, with
  `mcp-types`, `opentelemetry-api` and `sse-starlette`; §7 says what was and was not checked of them
  and gives their licences as their wheels state them, §9 and §13 the smoke step, and architecture
  Appendix C.2 and C.7 record them. DEPLOY, RUNBOOK, PENTEST and `SECURITY.md` §10 describe the server.

## 15. Honest limits, deferred

**Limits.**
- Builds are not reproducible: mtimes and pyc headers vary, and `SOURCE_DATE_EPOCH` fixes only config
  times.
- Provenance is the maintainer's attestation (SLSA Build L1-equivalent), with one online machine and a
  software key.
- amd64 is built and smoke-tested under emulation, while the tests run on arm64. Since the file
  reader's hardening that leaves more unobserved than before: the reader's data limit and its socket
  filter are not tried on amd64 by the build at all (under emulation the limit ends the reader as it
  starts and no filter is loaded), so an x86_64 kernel first enforces both on a site's host, in the
  self-test of DEPLOY's acceptance checklist (item 25). The filter's x86_64 arm is judged only by an
  interpreter of its program in the test suite.
- Verification was checked on Docker 29.1.3 with the classic store; the containerd store is untested
  (step 5 accepts the manifest digest for it, by reasoning, not by a run).
- That `BASES` belongs to `INDEX` was looked up once, and is not re-checked offline.
- Vendored libraries are decided by hand per file, not matched by a scanner. Inlined Vite helpers are
  missing; hatchling is not hash-locked; pip remains in the base layer; syft's SPDX names the vendor as
  every package's supplier.
- VEX is verified on grype only.
- CPE matches need recurring triage.
- `db` is not hardened.
- DuckDB ([FILES.md](FILES.md) §3) is native code with third-party libraries compiled in, which no SBOM
  entry, scanner or `*.libs` walk sees (§7): a vulnerability in one of them is found only through
  DuckDB's own advisories, by its version. Its manylinux wheels were not run or inspected by this build,
  and the release image's smoke step for it has run on the docker stub only. As amended on 2026-10-04:
  the aarch64 wheel was run natively, and the x86_64 one under emulation, by the reader's image tests in
  images built from the working tree (§13); the wheels' `*.libs` check (U1) is still open, and
  `release._smoke` has still not run on a real image. As amended by the rehearsal of 2026-10-04 (§7, §9
  step 5): U1 is closed, with no `*.libs` file on either machine, and `_smoke` ran for real on linux/amd64
  under emulation; natively it has not.
- The MCP library and the three packages it brought ([MCP.md](MCP.md) §3) are in no built image yet: no
  SBOM, grype or pip-audit run has seen them, and the release image's smoke step for the server has run
  on the docker stub only (§7, §9 step 5). The library's protocol and its transports are its own:
  DeltaSleuth pins one version exactly and observes it with that version's own client, in the
  development environment. The OpenTelemetry API it imports reads three environment names as it loads;
  a name set to something the image does not hold stops the server's start (DEPLOY section 11).

**Deferred.**
- cosign and registries;
- SLSA provenance;
- CycloneDX VEX, and CPE components for vendored libraries;
- a hardware key;
- reproducible builds;
- multi-platform bundles;
- DB role separation;
- TLS in `ds serve`;
- a public advisory feed;
- SOC 2.
