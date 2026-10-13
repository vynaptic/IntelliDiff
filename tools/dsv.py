"""``dsv``: the vendor's licence tool, outside the shipped product.

    uv run python tools/dsv.py keygen --out DIR
    uv run python tools/dsv.py issue --key file:/… --customer T --order T --installation I-… --tier T
        [--pairs N|unlimited] --starts YYYY-MM-DD --ends YYYY-MM-DD --out DIR --register FILE
    uv run python tools/dsv.py register --register FILE [--ending DAYS]

Run it on the maintainer's offline machine. It holds no secret: a licence is only as strong as the vendor's
private key, which never enters a repository. The key and its passphrase come only through ``file:``
references (absolute paths) to files the maintainer writes, or a passphrase prompt; never an argument value,
the environment or a chat. The image copies only ``deltasleuth/``, so no installation runs it.

It serves a vendor build. A checkout whose ``licence.SHIPPED_KEYS`` is empty builds an open build, which
needs no licence and enforces none: ``issue`` then refuses every key, since no build would trust its
licence. Pasting a ``keygen`` entry into ``SHIPPED_KEYS`` turns enforcement on in every build made from then
on, whose installations are unlicensed evaluations until a licence is installed.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from deltasleuth import licence
from deltasleuth.core.secrets import SecretError, read_secret, read_secret_bytes
from deltasleuth.licence import LicenceError
from deltasleuth.security.signing import PackSigner, SigningKeyError

PAIRS = {"community": "2", "pro": "5", "enterprise": "unlimited"}  # a tier's pairs unless --pairs
HELP = "The vendor's licence tool, for a build that embeds vendor keys: one with none needs no licence."
app = typer.Typer(no_args_is_help=True, help=HELP, pretty_exceptions_enable=False)
OutOpt = Annotated[Path, typer.Option(help="Output directory.")]
RegisterOpt = Annotated[Path, typer.Option(help="The register of genuine licences: one JSON line each.")]
PassOpt = Annotated[str | None, typer.Option(help="file:PATH of the key's passphrase (default: a prompt).")]


def fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(2)


def file_ref(ref: str, option: str) -> str:
    """``ref`` when it is ``file:`` and an absolute path."""
    if not ref.startswith("file:") or not Path(ref.removeprefix("file:")).is_absolute():
        fail(f"{option} must be file:<absolute path>")
    return ref


def passphrase(ref: str | None, *, confirm: bool = False) -> bytes:
    if ref is not None:
        try:
            return read_secret(file_ref(ref, "--passphrase")).encode()
        except ValueError as exc:
            fail(str(exc))
    secret = getpass.getpass("passphrase: ")
    if not secret or (confirm and getpass.getpass("again: ") != secret):
        fail("no passphrase, or the two differ")
    return secret.encode()


def create(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a new file: never over an existing one."""
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as handle:
        handle.write(data)


def keypair(out: Path, prefix: str, passphrase_ref: str | None) -> PackSigner:
    """Write a new key pair to OUT: <prefix>-<fp16>.key.pem (P-256, PKCS#8, encrypted, mode 0600) and
    <prefix>-<fp16>.pub.pem, never over a file. Refuses an OUT in a git working tree before asking."""
    out = out.resolve()
    if any((folder / ".git").exists() for folder in (out, *out.parents)):
        fail(f"{out} is in a git working tree: keep {prefix} keys outside every repository")
    encryption = serialization.BestAvailableEncryption(passphrase(passphrase_ref, confirm=True))
    key = ec.generate_private_key(ec.SECP256R1())
    signer = PackSigner(key)
    stem = f"{prefix}-{signer.fingerprint[:16]}"
    out.mkdir(parents=True, exist_ok=True)
    pkcs8 = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    create(out / f"{stem}.key.pem", pkcs8, 0o600)
    create(out / f"{stem}.pub.pem", signer.public_pem)
    return signer


@app.command()
def keygen(out: OutOpt, passphrase: PassOpt = None) -> None:
    """Write a new vendor key pair to DIR: vendor-<fp16>.key.pem (P-256, PKCS#8, encrypted, mode 0600) and
    vendor-<fp16>.pub.pem, and print its fingerprint and VENDOR_KEYS entry. Refuses a DIR in a git working
    tree. A build made with that entry in licence.SHIPPED_KEYS enforces licences; one with none there
    enforces no licence."""
    signer = keypair(out, "vendor", passphrase)
    stem = f"vendor-{signer.fingerprint[:16]}"
    # Double-quoted, as ruff format writes them: a PEM line holds no quote or backslash.
    entry = "".join(f'    b"{line}\\n"\n' for line in signer.public_pem.decode().splitlines())
    typer.echo(f"fingerprint: {signer.fingerprint}")
    typer.echo(f"wrote {stem}.key.pem (encrypted, mode 0600) and {stem}.pub.pem to {out.resolve()}")
    typer.echo(f"the VENDOR_KEYS entry (licence.SHIPPED_KEYS):\n{entry.rstrip()},")
    typer.echo(f"the line of tests/fixtures/vendor-keys.txt:\n{signer.fingerprint} trusted")


def signer(key: str, passphrase_ref: str | None) -> PackSigner:
    """The encrypted private key at ``key`` (``file:``), unlocked with the passphrase."""
    try:
        data = read_secret_bytes(file_ref(key, "--key"))
    except SecretError as exc:
        fail(f"--key: {exc}")
    try:
        return PackSigner(serialization.load_pem_private_key(data, password=passphrase(passphrase_ref)))  # type: ignore[arg-type]
    except SigningKeyError:
        fail("--key: expected an EC P-256 private key")
    except (ValueError, TypeError, UnsupportedAlgorithm):
        fail("--key: not an encrypted PEM private key, or not its passphrase")


@app.command()
def issue(
    key: Annotated[str, typer.Option(help="file:PATH of the vendor's private key (dsv keygen).")],
    customer: Annotated[str, typer.Option(help="The customer, as the order names it.")],
    order: Annotated[str, typer.Option(help="The order's reference.")],
    installation: Annotated[str, typer.Option(help="The installation id (ds licence id).")],
    tier: Annotated[str, typer.Option(help="community, pro or enterprise.")],
    starts: Annotated[str, typer.Option(help="The first day, YYYY-MM-DD (UTC).")],
    ends: Annotated[str, typer.Option(help="The last day, YYYY-MM-DD (UTC): at most 365 days later.")],
    out: OutOpt,
    register: RegisterOpt,
    pairs: Annotated[
        str | None, typer.Option(help="1 to 9999 or unlimited (default: 2, 5, unlimited).")
    ] = None,
    passphrase: PassOpt = None,
) -> None:
    """Sign a licence, write it to DIR as LI-….txt and append it to the register. Refuses a key the
    checkout's licence.SHIPPED_KEYS lacks: every key, while it embeds none."""
    for field, value in (("customer", customer), ("order", order)):
        if value != value.strip():
            fail(f"{field} begins or ends with a blank")
    vendor = signer(key, passphrase)
    fields = {
        "licence": licence.new_id(), "customer": customer, "order": order, "installation": installation,
        "tier": tier, "pairs": pairs or PAIRS.get(tier, ""), "starts": starts, "ends": ends,
        "issued": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}", "key": vendor.fingerprint,
    }  # fmt: skip
    body = licence.body(fields)
    data = f"{body}signature: {base64.b64encode(vendor.sign(body.encode())).decode()}\n".encode()
    try:
        issued = licence.parse(data, keys=(vendor.public_pem,), name="the licence")
    except LicenceError as exc:
        fail(str(exc))
    try:
        licence.parse(data)  # as this checkout's build reads it
    except LicenceError:
        fail(f"key {vendor.fingerprint[:12]}… is not in VENDOR_KEYS: no build trusts it yet")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{issued.licence_id}.txt"
    with register.open("a", encoding="utf-8") as handle:  # first: no genuine licence goes unregistered
        handle.write(json.dumps(issued.fields()) + "\n")
    create(path, data)
    typer.echo(f"issued {_described(issued.fields())}: {path}")


def _described(entry: dict[str, Any]) -> str:
    pairs = "unlimited" if entry["pairs"] is None else entry["pairs"]
    return (
        f"{entry['licence_id']} ({entry['tier']}, {entry['customer']}, order {entry['order']},"
        f" {entry['starts']} to {entry['ends']}, {pairs} pairs) for {entry['installation_id']}"
    )


@app.command("register")
def show_register(
    register: RegisterOpt,
    ending: Annotated[int | None, typer.Option(help="Only those ending within DAYS (renewals: 60).")] = None,
) -> None:
    """List what was issued, oldest first, or what ends within DAYS."""
    entries = [json.loads(line) for line in register.read_text(encoding="utf-8").splitlines() if line]
    today = datetime.now(UTC).date()
    for entry in entries:
        if ending is None or today <= date.fromisoformat(entry["ends"]) <= today + timedelta(days=ending):
            typer.echo(f"{_described(entry)}, issued {entry['issued']}")


if __name__ == "__main__":
    app()
