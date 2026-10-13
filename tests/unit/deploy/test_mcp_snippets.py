"""The client wiring of docs/mvp/MCP.md §13, which the RUNBOOK repeats block for block: each block parses and
names ``/v1/mcp`` or ``ds mcp``, the headers helper is valid sh, no block holds a token or reads one from an
environment variable (D13), what a block runs is a command line ``ds`` takes, and the host channel's starts
Compose in the installation's folder. Each client's own behaviour is not tested here (N8), nor is Compose
run. And §2: the guides say what MCP is not as ``ds mcp check`` prints it."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from deltasleuth.cli.main import app
from deltasleuth.mcp import tools

FENCED = re.compile(r"^```(\w+)\n(.*?)^```$", re.S | re.M)
SETTINGS = "database_dsn_ref: file:dsn\nevidence_dir: e\nworkspace_dir: w\ntargets_file: t.yaml\n"
OFF = "error: MCP is off: the settings have no mcp block (docs/mvp/MCP.md §4)\n"


def section(repo_root: Path, name: str, heading: str) -> str:
    """A document's ``##`` section, without the sections after it."""
    text = (repo_root / "docs" / "mvp" / name).read_text()
    return re.split(r"^## ", text.split(f"\n## {heading}\n", 1)[1], maxsplit=1, flags=re.M)[0]


@pytest.fixture(scope="module")
def wiring(repo_root: Path) -> str:
    return section(repo_root, "MCP.md", "13. Wiring a client (MCP.md and RUNBOOK; a test parses each block)")


def servers(wiring: str) -> list[dict[str, Any]]:
    """The one server each JSON block declares, under whichever name its client reads."""
    found = []
    for kind, text in FENCED.findall(wiring):
        if kind == "json":
            document = json.loads(text)
            (declared,) = (document.get("mcpServers") or document["servers"]).items()
            assert declared[0] == "deltasleuth" and set(document) <= {"mcpServers", "servers", "inputs"}
            found.append(declared[1] | {"inputs": document.get("inputs", [])})
    return found


def test_the_runbook_repeats_the_contract_s_blocks(repo_root: Path, wiring: str) -> None:
    blocks = FENCED.findall(wiring)
    assert [kind for kind, _ in blocks] == ["json", "sh", "json", "json", "json"]
    assert FENCED.findall(section(repo_root, "RUNBOOK.md", "Your own agent (MCP)")) == blocks


def test_each_block_parses_and_names_the_server(wiring: str) -> None:
    http, prompted, developer, stdio = servers(wiring)
    assert [server["url"] for server in (http, prompted, developer)] == [
        "https://deltasleuth.bank.example/v1/mcp",
        "https://deltasleuth.bank.example/v1/mcp",
        "http://127.0.0.1:8000/v1/mcp",  # developer mode: loopback, and the header its POST needs
    ]
    assert (http["type"], prompted["type"], developer["headers"]) == ("http", "http", {"X-CSRF-Token": "dev"})
    assert (stdio["command"], stdio["args"][3:5]) == ("uv", ["ds", "mcp"]) and "url" not in stdio
    (helper,) = [text for kind, text in FENCED.findall(wiring) if kind == "sh"]
    checked = subprocess.run(["sh", "-n"], input=helper, capture_output=True, text=True, check=False)
    assert (checked.returncode, checked.stderr) == (0, "") and helper.startswith("#!/bin/sh\n")
    assert http["headersHelper"].endswith("/mcp-headers") and "mcp-headers" in helper


def test_no_block_holds_a_token_or_reads_one_from_the_environment(wiring: str) -> None:
    """D13: the token is a file's one line (the helper's ``cat``) or the editor's own prompt, never text in
    a block and never a variable of the client's environment."""
    http, prompted, developer, stdio = servers(wiring)
    assert not [text for _, text in FENCED.findall(wiring) if "dst_" in text]
    assert not [server for server in (http, prompted, developer, stdio) if "env" in server]
    credentials = [server.get("headers", {}).get("Authorization") for server in (http, prompted, developer)]
    assert credentials == [None, "Bearer ${input:ds-token}", None]  # the helper's; the prompt's; none
    held = [value for server in (http, prompted, developer) for value in server.get("headers", {}).values()]
    assert [value for value in held if "${" in value.replace("${input:", "")] == []
    assert prompted["inputs"] == [
        {"type": "promptString", "id": "ds-token", "description": "DeltaSleuth API token", "password": True}
    ]
    (helper,) = [text for kind, text in FENCED.findall(wiring) if kind == "sh"]
    assert re.findall(r"\$\{?(\w+)", helper) == ["HOME"] and '"$(cat "$HOME/' in helper  # a file, by its path


def test_what_a_block_runs_is_a_command_ds_takes(
    wiring: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stdio block's and the host channel's ``ds`` arguments, given to the CLI itself over settings
    without an ``mcp`` block: each is parsed whole, and refused only for that."""
    (tmp_path / "ds.yaml").write_text(SETTINGS)
    *_, stdio = servers(wiring)
    assert stdio["args"][:3] == ["run", "--directory", "/path/to/deltasleuth"]
    developer = [
        str(tmp_path / "ds.yaml") if arg == "/path/to/deltasleuth.yaml" else arg for arg in stdio["args"]
    ]
    ran = CliRunner().invoke(app, developer[4:])
    assert (ran.exit_code, ran.output) == (2, OFF)
    # The host channel is written into the text, not as a block: a person's, never an agent's.
    said = " ".join(wiring.split())
    (hosted,) = re.findall(r'`"args": (\["-c", "[^"]*--host-channel"\])`', said)
    _, line = json.loads(hosted)
    compose = shlex.split(line)
    assert compose[:4] == ["cd", "/srv/deltasleuth", "&&", "exec"]
    assert compose[4:12] == ["docker", "compose", "run", "--rm", "-T", "-e", "DS_OPERATOR", "app"]
    assert '`"command": "sh"`' in said and '`"env": {"DS_OPERATOR": "jdoe"}`' in said
    monkeypatch.setenv("DS_CONFIG", str(tmp_path / "ds.yaml"))
    ran = CliRunner().invoke(app, compose[13:])
    assert (compose[12], ran.exit_code, ran.output) == ("ds", 2, OFF)


def test_the_host_channel_starts_compose_in_the_installation_s_folder(wiring: str, tmp_path: Path) -> None:
    """Compose reads ``.env``, which names the installation's compose files, from its working directory
    (DEPLOY), and a client starts a server from a directory of its own: the line changes to the folder
    first, then hands the client's stdin and ``DS_OPERATOR`` to ``docker compose``. A stand-in on the path
    says what it was started with: Compose itself is not run here."""
    (hosted,) = re.findall(r'`"args": (\["-c", "[^"]*--host-channel"\])`', " ".join(wiring.split()))
    flag, line = json.loads(hosted)
    folder, elsewhere, tools_dir = (tmp_path / name for name in ("srv", "elsewhere", "bin"))
    for made in (folder, elsewhere, tools_dir):
        made.mkdir()
    (tools_dir / "docker").write_text('#!/bin/sh\necho "$(pwd -P)|$DS_OPERATOR|$*"\ncat\n')
    (tools_dir / "docker").chmod(0o755)
    env = {"PATH": f"{tools_dir}{os.pathsep}{os.environ['PATH']}", "DS_OPERATOR": "jdoe"}
    ran = subprocess.run(
        ["sh", flag, line.replace("/srv/deltasleuth", shlex.quote(str(folder)))],
        cwd=elsewhere, env=env, input="a frame\n", capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert (ran.returncode, ran.stderr) == (0, "")
    assert ran.stdout == (
        f"{folder.resolve()}|jdoe|compose run --rm -T -e DS_OPERATOR app ds mcp --host-channel\na frame\n"
    )


def test_the_guides_say_what_mcp_is_not(repo_root: Path) -> None:
    """§2: the RUNBOOK and DEPLOY hold each statement of ``mcp.tools.NOT`` word for word, code marks and
    line breaks aside."""
    for name, heading in (("RUNBOOK.md", "Your own agent (MCP)"), ("DEPLOY.md", "3. Configure")):
        said = " ".join(section(repo_root, name, heading).replace("`", "").split())
        assert [line for line in tools.NOT if line not in said] == [], name
