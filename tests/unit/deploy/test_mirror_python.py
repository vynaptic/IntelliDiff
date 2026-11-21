"""The build installs hash-locked wheels from one PEP 503 mirror."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from zipfile import ZipFile

import pytest


class _PrivateIndex(SimpleHTTPRequestHandler):
    def _authed(self) -> bool:
        expected = "Basic " + base64.b64encode(b"builder:mirror-test").decode()
        if self.headers.get("Authorization") == expected:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="test"')
        self.end_headers()
        return False

    def do_GET(self) -> None:
        if self._authed():
            super().do_GET()

    def do_HEAD(self) -> None:
        if self._authed():
            super().do_HEAD()


def _wheel(root: Path, name: str, module: str) -> Path:
    stem = name.replace("-", "_")
    directory = root / name
    directory.mkdir()
    wheel = directory / f"{stem}-1.0-py3-none-any.whl"
    dist = f"{stem}-1.0.dist-info"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{stem}.py", module)
        archive.writestr(f"{dist}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
        archive.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        members = [*archive.namelist(), f"{dist}/RECORD"]
        archive.writestr(f"{dist}/RECORD", "\n".join(f"{member},," for member in members))
    (directory / "index.html").write_text(f'<a href="{wheel.name}">{wheel.name}</a>')
    return wheel


def test_mirror_wheel_build_uses_the_mirror_and_checks_the_lock(repo_root: Path, tmp_path: Path) -> None:
    dep = _wheel(tmp_path, "mirror-dep", "value = 1")
    backend = _wheel(tmp_path, "mirror-backend", """
from pathlib import Path
from shutil import copy
def build_editable(wheel_directory):
    wheel, = Path("mirror-app").glob("*.whl")
    copy(wheel, wheel_directory)
    return wheel.name
""")
    editable = _wheel(tmp_path, "mirror-app", "")
    source = tmp_path / "src"
    source.mkdir()
    (source / "mirror_app_source.py").write_text("value = 1")
    with ZipFile(editable, "a") as archive:
        archive.writestr("mirror-app.pth", str(source))

    digest = hashlib.sha256(dep.read_bytes()).hexdigest()
    (tmp_path / "pyproject.toml").write_text("""
[project]
name = "mirror-app"
version = "1.0"
requires-python = ">=3.13"
dependencies = ["mirror-dep==1.0"]
[build-system]
requires = ["mirror-backend==1.0"]
build-backend = "mirror_backend"
""")
    (tmp_path / "uv.lock").write_text(f"""
version = 1
revision = 3
requires-python = ">=3.13"
[[package]]
name = "mirror-app"
version = "1.0"
source = {{ editable = "." }}
dependencies = [{{ name = "mirror-dep" }}]
[[package]]
name = "mirror-dep"
version = "1.0"
source = {{ registry = "https://unused.invalid/simple" }}
wheels = [{{ url = "https://unused.invalid/{dep.name}", hash = "sha256:{digest}" }}]
""")
    script = tmp_path / "mirror-python.sh"
    shutil.copy(repo_root / "deploy" / script.name, script)
    (tmp_path / "uv-requirements.txt").write_text(f"mirror-dep==1.0 --hash=sha256:{digest}\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".netrc").write_text("machine 127.0.0.1 login builder password mirror-test\n")
    (home / ".netrc").chmod(0o600)
    (tmp_path / "mirror-build-requirements.txt").write_text(
        f"mirror-backend==1.0 --hash=sha256:{hashlib.sha256(backend.read_bytes()).hexdigest()}\n"
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), partial(_PrivateIndex, directory=tmp_path)) as http:
        thread = Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            env = os.environ | {
                "MIRROR_PYPI": f"http://127.0.0.1:{http.server_port}/",
                "UV_NO_INDEX": "1", "UV_EXTRA_INDEX_URL": "https://unused.invalid/simple",
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                "NO_PROXY": "127.0.0.1", "HOME": str(home),
            }
            bootstrap = tmp_path / "bootstrap"
            subprocess.run([sys.executable, "-m", "venv", str(bootstrap)], check=True)
            subprocess.run(["sh", str(script), "bootstrap"], cwd=tmp_path, check=True,
                           env=env | {"PATH": str(bootstrap / "bin") + os.pathsep + env["PATH"]})
            subprocess.run([str(bootstrap / "bin/python"), "-I", "-c", "import mirror_dep"], check=True)
            for stage in ("dependencies", "project"):
                subprocess.run(["sh", str(script), stage], cwd=tmp_path, env=env, check=True)
            subprocess.run(
                [str(tmp_path / ".venv/bin/python"), "-I", "-c",
                 "import mirror_app_source, mirror_dep, importlib.util\n"
                 "assert not importlib.util.find_spec('mirror_backend')"],
                cwd=tmp_path, check=True,
            )
            shutil.rmtree(tmp_path / ".venv")
            with ZipFile(dep, "a") as changed:
                changed.writestr("changed.txt", "altered after locking")
            failed = subprocess.run(["sh", str(script), "dependencies"], cwd=tmp_path, env=env,
                                    capture_output=True, text=True, check=False)
            assert failed.returncode and "hash" in failed.stderr.lower()
        finally:
            http.shutdown()
            thread.join()


@pytest.mark.parametrize("url", ["", "http://mirror.example/simple", "https://user:private@mirror.example"])
def test_mirror_index_refusals_hide_credentials(repo_root: Path, url: str) -> None:
    result = subprocess.run(
        ["sh", str(repo_root / "deploy/mirror-python.sh"), "bootstrap"],
        env=os.environ | {"MIRROR_PYPI": url}, capture_output=True, text=True, check=False,
    )
    assert result.returncode and "MIRROR_PYPI" in result.stderr and "private" not in result.stderr
