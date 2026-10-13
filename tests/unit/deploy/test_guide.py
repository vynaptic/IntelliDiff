"""The user guide's install chapters (docs/mvp/FIRSTRUN.md §10 to §12): chapter 1 and its appendix show every
step deploy/up.sh takes and quote what it prints word for word, and a second installation is edits of the
examples' own lines, never ``sed``. And of every chapter in docs/guide, those added later too: its links lead
to files that exist and that a release kit holds, a step that needs more than the kit says so, and no path
of the machine it was written on is left in it."""

from __future__ import annotations

import re
from getpass import getuser
from pathlib import Path

from tests.unit.deploy.test_up_script import TEN, Trial
from tools import release

INSTALL = ("01-install-docker.md", "A1-install-by-hand.md")  # the script's chapter, and its steps by hand
FENCED = re.compile(r"^ *```(\w*)\n(.*?)^ *```$", re.S | re.M)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
QUOTED = r"""(?:'([^']*)'|"((?:\\.|[^"\\])*)")"""  # a shell word in single or in double quotes
VARIABLES = re.compile(r"(?:\$\{[^}]*\}|(?<!\\)\$\w+)+")  # a run of them, as double quotes hold them
HOST = ("/Users/", "/home/", "/private/", "/var/folders/")  # as in a bundle (tests/unit/test_release_tool.py)


def _chapters(repo_root: Path) -> list[Path]:
    return sorted((repo_root / "docs" / "guide").glob("*.md"))


def _install(repo_root: Path) -> list[str]:
    return [(repo_root / "docs" / "guide" / name).read_text() for name in INSTALL]


def _blocks(*pages: str, kind: str = "") -> list[str]:
    """The pages' fenced blocks: all of them, or those of one kind (``bash`` is a command)."""
    return [block for page in pages for found, block in FENCED.findall(page) if kind in ("", found)]


def _links(page: Path) -> list[tuple[Path, str]]:
    """Where each relative link of a page leads: the file and, if the link names one, the heading's anchor."""
    targets = LINK.findall(FENCED.sub("", page.read_text()))
    led = [target.partition("#") for target in targets if not re.match(r"[a-z]+:|#", target)]
    return [((page.parent / path).resolve(), anchor) for path, _, anchor in led]


def _led(repo_root: Path) -> list[tuple[Path, str]]:
    """Every relative link of the guide, and the README's links into it."""
    guide = (repo_root / "docs" / "guide").resolve()
    links = [link for page in _chapters(repo_root) for link in _links(page)]
    return links + [link for link in _links(repo_root / "README.md") if guide in link[0].parents]


def _said(script: str) -> list[re.Pattern[str]]:
    """What deploy/up.sh ends or closes with, a line a pattern: each refusal, its one warning and its closing
    block, the texts it names in capitals written out and any other variable as any text."""
    named = re.findall(rf"^([A-Z]+)=(?:{QUOTED}|(\S+))", script, re.M)
    texts = {name: plain or text or bare for name, plain, text, bare in named}
    script = re.sub(r"\$([A-Z]+)\b", lambda found: texts.get(found[1], found[0]), script)
    lines = [f"error: {plain or text}" for plain, text in re.findall(rf"\bdie {QUOTED}", script)]
    lines += [f"warning: {text}" for _, text in re.findall(rf"printf 'warning: %s\\n' {QUOTED}", script)]
    lines += filter(None, script.rpartition("\ncat << EOF\n")[2].partition("\nEOF\n")[0].split("\n"))
    parts = [VARIABLES.split(re.sub(r'\\(["`\\])', r"\1", line.strip())) for line in lines]
    return [re.compile(".+".join(re.escape(part.replace("\\$", "$")) for part in line)) for line in parts]


def test_the_guide_shows_every_step_up_sh_takes(repo_root: Path, tmp_path: Path) -> None:
    """Each ``docker compose`` command of a fresh run, from step 1 on, is in a code block of chapter 1 or of
    its appendix as the script types it, the caller's name as ``"$(id -un)"``; so is each file it makes."""
    trial = Trial(tmp_path, "sh")
    before = trial.tree()
    ran = trial.run()
    assert ran.code == 0 and trial.created(before) == sorted(TEN)
    composed = [" ".join(["docker", *argv]) for argv in ran.docker() if argv[0] == "compose"]
    steps = composed[composed.index("docker compose --profile mocks config") :]  # step 1's own check
    me = re.compile(rf"(DS_OPERATOR=|--owner ){re.escape(getuser())} ")  # the caller, where a step names it
    steps = [me.sub(r'\1"$(id -un)" ', step) for step in steps]
    blocks = _blocks(*_install(repo_root))
    assert [step for step in steps if not any(step in block for block in blocks)] == []
    whole = {name: re.compile(rf"(?<![\w./-]){re.escape(name)}(?![\w./-])") for name in TEN}
    assert [name for name in TEN if not any(whole[name].search(block) for block in blocks)] == []


def test_the_guide_needs_no_sed_before_a_second_installation(repo_root: Path) -> None:
    """§4 and §11: by hand an installation is five copies, and a second one the script's three options or four
    lines of three files, each an example's own line switched on (its ``# `` removed) with the reader's
    value in the example's place. No code block of chapter 1 or of its appendix runs sed."""
    blocks = _blocks(*_install(repo_root))
    assert [block for block in blocks if re.search(r"\bsed\b", block)] == []

    def line(example: str, holds: str, **written: str) -> str:
        lines = (repo_root / "deploy" / example).read_text().splitlines()
        [found] = [text for text in lines if holds in text]
        assert all(value in found for value in written.values())
        for name, value in written.items():
            found = found.replace(value, f"<{name}>")
        return found.replace("# ", "", 1) if found.lstrip().startswith("# ") else found

    site = "docker-compose.site.example.yml"
    typed = [
        "cp deploy/config/deltasleuth.example.yaml deploy/config/deltasleuth.yaml",
        "cp deploy/config/targets.example.yaml deploy/config/targets.yaml",
        "cp deploy/config/llm_profiles.example.yaml deploy/config/llm_profiles.yaml",
        f"cp deploy/{site} deploy/docker-compose.site.yml",
        "cp deploy/env.source.example .env",
        "sh deploy/up.sh --project <project> --port <port> --subnet <subnet>",
        line("env.source.example", "COMPOSE_PROJECT_NAME=", project="deltasleuth2"),
        line(site, "ports: !override", port="8001"),
        line(site, "networks: {", subnet="10.99.0.0/24", gateway="10.99.0.1"),
        line("config/deltasleuth.example.yaml", "  public_url: ", port="8000"),
    ]
    assert [text for text in typed if not any(f"{text}\n" in block for block in blocks)] == []


def test_guide_links_resolve_inside_the_kit(repo_root: Path) -> None:
    """A release's source archive holds ``release.SHIPPED`` and nothing else (docs/mvp/RELEASE.md §2): every
    relative link of the guide, and each of the README's into it, names a path under one of those entries."""
    kit = {(repo_root / entry).resolve() for entry in release.SHIPPED}
    links = _led(repo_root)
    assert links and [str(path) for path, _ in links if not kit & {path, *path.parents}] == []


def test_every_guide_link_leads_to_a_file_that_exists(repo_root: Path) -> None:
    """And to one of that file's headings, where the link names one, by the anchor GitHub gives a heading."""
    for path, anchor in _led(repo_root):
        assert path.is_file(), path
        headings = re.findall(r"^#+ (.+)$", path.read_text(), re.M) if anchor else []
        anchors = {re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-") for heading in headings}
        assert not anchor or anchor in anchors, (path, anchor)


def test_a_guide_step_outside_the_kit_says_so(repo_root: Path) -> None:
    """A kit holds no ``tests`` and no ``tools`` (§10): a section whose commands name either says that it
    needs a git checkout, as chapter 2's lint and tests do."""
    pages = [page.read_text() for page in _chapters(repo_root)]
    sections = [section for page in pages for section in re.split(r"^## ", page, flags=re.M)]
    # The two folders as a command names them: not an MCP method (tools/list) or a model's "tools".
    named = re.compile(
        r"\bpytest\b|\bruff\b[^\n]*\b(?:tests|tools)\b|\b(?:tests|tools)/(?!list\b|call\b)[\w.-]"
    )
    outside = [section for section in sections if any(map(named.search, _blocks(section, kind="bash")))]
    unsaid = [section.partition("\n")[0] for section in outside if "needs a git checkout" not in section]
    assert outside and unsaid == []


def test_the_guide_holds_no_host_path(repo_root: Path) -> None:
    """A folder of the writer's machine is written ``<checkout>`` or ``<folder>``, never as it was."""
    pages = {page.name: page.read_text() for page in _chapters(repo_root)}
    assert [(name, path) for name, text in pages.items() for path in HOST if path in text] == []


def test_the_guide_quotes_the_script_word_for_word(repo_root: Path) -> None:
    """Every line that heads an entry of chapter 1, or of the appendix's sections on the script, as its
    ``error:`` or ``warning:``, and every line of the closing block that a chapter shows, is in deploy/up.sh
    word for word, with a value where the script has a variable."""
    said = _said((repo_root / "deploy" / "up.sh").read_text())
    chapter, appendix = _install(repo_root)
    sections = "".join(re.findall(r"^### The script .*?(?=^##)", appendix, re.S | re.M))
    for page in (chapter, sections):
        heads = re.findall(r"\*\*`((?:error|warning):\s[^`]+)`\*\*", page)
        heads = [" ".join(head.split()) for head in heads]  # a quoted line breaks where the page wraps
        assert heads and [head for head in heads if not any(line.fullmatch(head) for line in said)] == []
    shown = []
    for block in _blocks(*(page.read_text() for page in _chapters(repo_root))):
        _, closing, rest = block.partition("DeltaSleuth is running:")
        shown += [line.strip() for line in (closing + rest).splitlines() if line.strip() not in ("", "...")]
    assert shown and [line for line in shown if not any(known.fullmatch(line) for known in said)] == []
