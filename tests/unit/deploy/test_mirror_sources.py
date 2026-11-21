"""The build uses only the named mirrors and preserves artifact verification."""

import base64
import hashlib
import io
import json
import os
import runpy
import shutil
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy"


def test_ui_compiles_without_network_access() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text()
    assert "RUN --network=none npm --offline --update-notifier=false run build" in dockerfile


def test_apt_uses_only_the_mirror_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    debian = "https://packages.example/artifactory/debian"
    security = "https://packages.example/artifactory/debian-security"
    monkeypatch.setenv("MIRROR_DEBIAN", debian + "/")
    monkeypatch.setenv("MIRROR_DEBIAN_SECURITY", security)
    runpy.run_path(str(DEPLOY / "mirror-apt.py"))["configure"](tmp_path)
    signed = "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg]"
    assert (tmp_path / "mirror-apt.list").read_text().splitlines() == [
        f"{signed} {debian} trixie main", f"{signed} {debian} trixie-updates main",
        f"{signed} {security} trixie-security main",
    ]
    config = (tmp_path / "mirror-apt.conf").read_text()
    assert f'Dir::Etc::sourcelist "{tmp_path}/mirror-apt.list"' in config
    assert 'Dir::Etc::sourceparts "-"' in config
    assert 'APT::Update::Error-Mode "any"' in config


@pytest.mark.parametrize("url", ["", "http://mirror.example/repository", "https://user:word@mirror.example"])
def test_apt_rejects_unusable_or_credentialed_source(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIRROR_DEBIAN", "https://mirror.example/main")
    monkeypatch.setenv("MIRROR_DEBIAN_SECURITY", url)
    with pytest.raises(SystemExit, match="MIRROR_DEBIAN_SECURITY"):
        runpy.run_path(str(DEPLOY / "mirror-apt.py"))["configure"](tmp_path)
    assert not list(tmp_path.iterdir())


def _tarball() -> bytes:
    content = json.dumps({
        "name": "@mirror/fixture", "version": "1.0.0", "scripts": {"postinstall": "exit 1"},
    }).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("package/package.json")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


@pytest.mark.parametrize("case", ["available", "missing", "corrupt"])
def test_npm_installs_verified_tarball_only_from_mirror(case: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    if not (node and shutil.which("npm")):
        pytest.skip("Node.js and npm run the mirror frontend build")
    content = _tarball()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()
    requested: list[str] = []
    tarball = "@mirror/fixture/-/fixture-1.0.0.tgz"
    root = {"name": "trial", "dependencies": {"@mirror/fixture": "1.0.0"}}
    lock = {"name": "trial", "lockfileVersion": 3, "packages": {
        "": root, "node_modules/@mirror/fixture": {
            "version": "1.0.0", "resolved": f"https://registry.npmjs.org/{tarball}", "integrity": integrity,
        },
    }}
    (tmp_path / "package.json").write_text(json.dumps(root))
    (tmp_path / "package-lock.json").write_text(json.dumps(lock))

    class Registry(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            requested.append(self.path)
            if case == "missing" or self.path != "/artifactory/api/npm/site/" + tarball:
                self.send_error(404)
                return
            body = content if case == "available" else b"changed"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Registry) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        address = f"http://127.0.0.1:{server.server_port}"
        try:
            result = subprocess.run([node, str(DEPLOY / "mirror-npm.mjs")], cwd=tmp_path, env={
                "HOME": str(tmp_path), "PATH": os.environ["PATH"], "npm_config_fetch_retries": "0",
                "MIRROR_NPM": address + "/artifactory/api/npm/site", "NO_PROXY": "127.0.0.1",
                "HTTPS_PROXY": address, "HTTP_PROXY": address,
            }, capture_output=True, text=True, timeout=20, check=False)
        finally:
            server.shutdown()
    assert requested and set(requested) == {"/artifactory/api/npm/site/" + tarball}
    assert (result.returncode == 0) == (case == "available"), result.stderr
    if case == "available":
        assert (tmp_path / "node_modules/@mirror/fixture/package.json").is_file()
    elif case == "corrupt":
        assert "EINTEGRITY" in result.stderr
