"""The Agent Skill's generated references must match the current CLI."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "skills" / "igctl"


def _generator():
    spec = importlib.util.spec_from_file_location(
        "gen_skill_reference", REPO / "scripts" / "gen_skill_reference.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("IGCTL_HOME", str(tmp_path))


@pytest.mark.parametrize("name", ["commands.md", "automation.md"])
def test_references_up_to_date(name):
    gen = _generator()
    expected = gen.render() if name == "commands.md" else gen.render_automation()
    actual = (SKILL / "references" / name).read_text(encoding="utf-8")
    assert actual == expected, "Run `uv run python scripts/gen_skill_reference.py`."


def test_skill_frontmatter_is_valid():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with YAML frontmatter"
    import yaml

    meta = yaml.safe_load(m.group(1))
    # Agent Skills spec: lowercase/hyphen name matching the directory, description <= 1024.
    assert meta["name"] == SKILL.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta["name"])
    assert 0 < len(meta["description"]) <= 1024


def test_skill_commands_exist():
    """Every `igctl <group> <command>` mentioned in SKILL.md is a real command."""
    import typer.main

    from igctl.cli.main import app

    root = typer.main.get_command(app)
    ctx = root.context_class(root)
    real = set()
    for g in root.list_commands(ctx):
        group = root.get_command(ctx, g)
        if hasattr(group, "list_commands"):
            real |= {f"{g} {c}" for c in group.list_commands(group.context_class(group))}
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    globals_re = r"(?:\s+(?:--json|--dry-run|-[ab]\s+\S+|--account\s+\S+|--backend\s+\S+))*"
    used = set(re.findall(rf"`igctl{globals_re}\s+([a-z-]+ [a-z-]+)", text))
    groups = {c.split()[0] for c in real}
    missing = {u for u in used if u.split()[0] in groups and u not in real}
    assert not missing, f"SKILL.md references unknown commands: {sorted(missing)}"
