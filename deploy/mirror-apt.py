"""Write the signed trixie sources for builds that use package mirrors."""

import os
from pathlib import Path
from urllib.parse import urlsplit


def mirror(name: str) -> str:
    url = os.environ.get(name, "")
    parts = urlsplit(url)
    loopback_http = parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        not (parts.scheme == "https" or loopback_http) or not parts.hostname or parts.username is not None
        or parts.password is not None or parts.query or parts.fragment or "\\" in url or url.split() != [url]
    ):
        raise SystemExit(f"{name} must be a credential-free HTTPS repository URL")
    return url.rstrip("/")


def configure(directory: Path) -> None:
    debian, security = mirror("MIRROR_DEBIAN"), mirror("MIRROR_DEBIAN_SECURITY")
    signed = "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg]"
    sources = directory / "mirror-apt.list"
    sources.write_text(
        f"{signed} {debian} trixie main\n{signed} {debian} trixie-updates main\n"
        f"{signed} {security} trixie-security main\n", encoding="utf-8",
    )
    ca = Path("/run/secrets/mirror_ca")
    (directory / "mirror-apt.conf").write_text(
        f'Dir::Etc::sourcelist "{sources}";\nDir::Etc::sourceparts "-";\n'
        'APT::Update::Error-Mode "any";\nAcquire::https::AllowRedirect "false";\n'
        'Acquire::http::AllowRedirect "false";\n'
        + (f'Acquire::https::CAInfo "{ca}";\n' if ca.is_file() and ca.stat().st_size else ""),
        encoding="utf-8",
    )


if __name__ == "__main__":
    configure(Path("/tmp"))  # noqa: S108 - the Docker build stage reads both files from this directory
