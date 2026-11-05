"""
Files the Claude agents use: scripts, skills and the assistant's workdir template.

A beamline package adds its own text to a skill through the
``nbs_agents.skill_addenda`` entry point group: each entry names a folder
holding ``<skill-name>.md`` files, which are appended to that skill's
``SKILL.md`` (in entry point name order). ``claude-beamline`` writes the
combined skills into the agent's working folder with this module:

    python -m nbs_agents.assistant.resources skill NAME     # .claude/skills/NAME/SKILL.md
    python -m nbs_agents.assistant.resources prompt NAME    # .claude/NAME.md, path printed
    python -m nbs_agents.assistant.resources code-paths     # folders the agent's python needs
    python -m nbs_agents.assistant.resources sandbox-paths  # files beamline packages need, PATH<TAB>VAR

A beamline package names files its nbs-cli needs inside the bwclaude sandbox
through the ``nbs_agents.sandbox_paths`` group: each entry is a callable
``paths(config)`` returning ``(path, variable)`` pairs; the file is mounted
read-only and the ``NBS_*`` variable set to its path.

The ``claude-beamline`` console script runs the bundled script with
``NBS_ENV_DIR`` set to this python environment.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE / "scripts"
SKILLS_DIR = HERE / "skills"
WORKDIR_TEMPLATE = HERE / "templates" / "assistant"
SKILL_ADDENDA_ENTRY_POINT = "nbs_agents.skill_addenda"
SANDBOX_PATHS_ENTRY_POINT = "nbs_agents.sandbox_paths"


def addenda_dirs() -> list[Path]:
    """Folders of skill addenda from installed beamline packages, in entry point name order."""
    from importlib.metadata import entry_points

    return [Path(entry.load()) for entry in sorted(entry_points(group=SKILL_ADDENDA_ENTRY_POINT), key=lambda e: e.name)]


def skill_text(name: str) -> str:
    """
    A skill's ``SKILL.md`` with every beamline addendum for it appended.

    Parameters
    ----------
    name : str
        Skill name, such as ``"beamline-steering"``.

    Returns
    -------
    str
        The combined text.
    """
    parts = [(SKILLS_DIR / name / "SKILL.md").read_text().rstrip("\n")]
    for folder in addenda_dirs():
        addendum = folder / f"{name}.md"
        if addendum.is_file():
            parts.append(addendum.read_text().rstrip("\n"))
    return "\n\n".join(parts) + "\n"


def _write(path: Path, text: str) -> Path:
    """Write ``text`` unless the file already holds it."""
    if path.is_symlink():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file() or path.read_text() != text:
        path.write_text(text)
    return path


def install_skill(name: str, workdir: Optional[str] = None) -> Path:
    """
    Write a skill into ``workdir/.claude/skills/NAME/SKILL.md``.

    Replaces the symlink to the skill folder that earlier versions made.

    Returns
    -------
    Path
        The skill folder.
    """
    folder = Path(workdir or ".") / ".claude" / "skills" / name
    if folder.is_symlink():
        folder.unlink()
    _write(folder / "SKILL.md", skill_text(name))
    return folder


def write_prompt(name: str, workdir: Optional[str] = None) -> Path:
    """
    Write a skill as a system prompt file, ``workdir/.claude/NAME.md``.

    Returns
    -------
    Path
        The file.
    """
    return _write(Path(workdir or ".") / ".claude" / f"{name}.md", skill_text(name))


def code_paths() -> list[Path]:
    """
    Folders a sandboxed agent needs read access to for this python to work:
    the environment and the source of every editable install in it.

    Returns
    -------
    list of Path
        Resolved folders, none inside another.
    """
    from importlib.metadata import distributions

    found = {Path(sys.prefix).resolve()}
    for dist in distributions():
        try:
            info = json.loads(dist.read_text("direct_url.json") or "{}")
        except (OSError, ValueError):
            continue
        url = urlsplit(info.get("url", ""))
        if (info.get("dir_info") or {}).get("editable") and url.scheme == "file":
            found.add(Path(unquote(url.path)).resolve())
    ordered = sorted(found, key=lambda p: len(p.parts))
    out: list[Path] = []
    for path in ordered:
        if not any(path.is_relative_to(kept) for kept in out):
            out.append(path)
    return out


def sandbox_paths(config=None) -> list[tuple[str, str]]:
    """
    Files beamline packages need inside the sandbox, as ``(path, variable)`` pairs.

    Parameters
    ----------
    config : CliConfig, optional
        Settings; loaded if omitted.
    """
    from importlib.metadata import entry_points

    if config is None:
        from ..cli.config import CliConfig

        config = CliConfig.load()
    out = []
    for entry in sorted(entry_points(group=SANDBOX_PATHS_ENTRY_POINT), key=lambda e: e.name):
        out.extend((str(path), str(var)) for path, var in entry.load()(config) or [])
    return out


def claude_beamline() -> None:
    """The ``claude-beamline`` console script: run the bundled script with this environment."""
    os.environ.setdefault("NBS_ENV_DIR", sys.prefix)
    script = str(SCRIPTS_DIR / "claude-beamline")
    os.execv(script, [script, *sys.argv[1:]])


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["skill"] and len(args) == 2:
        install_skill(args[1])
    elif args[:1] == ["prompt"] and len(args) == 2:
        print(write_prompt(args[1]).resolve())
    elif args == ["sandbox-paths"]:
        for path, var in sandbox_paths():
            print(f"{path}\t{var}")
    elif args == ["code-paths"]:
        for path in code_paths():
            print(path)
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
