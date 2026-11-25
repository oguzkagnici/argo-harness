"""Progressive-disclosure skills: markdown instructions loaded on demand.

Skill layout (compatible with Claude/Codex-style skills):
  .argo/skills/<name>/SKILL.md   (YAML frontmatter: name, description; then instructions)
  .argo/skills/<name>.md
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str


def _parse(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8", errors="replace")
    name, desc = path.parent.name if path.name == "SKILL.md" else path.stem, ""
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip().strip('"\'')
            elif line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"\'')
    if not desc:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith("#")), "")
        desc = first[:160]
    return Skill(name=name, description=desc, path=path, body=body.strip())


def discover(ctx: ToolContext) -> list[Skill]:
    roots = [Path(ctx.cfg.skill_dir)]
    try:
        roots.append(Path(__file__).resolve().parents[2] / "skills")
    except Exception:
        pass
    found: dict[str, Skill] = {}
    for root in roots:
        if not root.exists():
            continue
        for p in list(root.glob("*/SKILL.md")) + list(root.glob("*.md")):
            try:
                sk = _parse(p)
                found.setdefault(sk.name, sk)
            except OSError:
                continue
    return sorted(found.values(), key=lambda s: s.name)


def skill_index(ctx: ToolContext) -> str:
    skills = discover(ctx)
    if not skills:
        return ""
    return "\n".join(f"- {s.name}: {s.description}" for s in skills)


def _list(ctx: ToolContext) -> ToolResult:
    skills = discover(ctx)
    if not skills:
        return ToolResult("No skills installed. Add .argo/skills/<name>/SKILL.md")
    return ToolResult("Available skills:\n" + "\n".join(f"- {s.name}: {s.description} ({s.path})" for s in skills))


def _read(ctx: ToolContext, name: str) -> ToolResult:
    for s in discover(ctx):
        if s.name == name or s.name.lower() == name.lower():
            return ToolResult(f"# Skill: {s.name}\nPath: {s.path}\n\n{s.body}")
    return ToolResult(f"Skill '{name}' not found. Use list_skills.", is_error=True)


def register(reg: ToolRegistry) -> None:
    reg.add("list_skills", "List installed skills with descriptions.",
            {"type": "object", "properties": {}}, _list, read_only=True)
    reg.add("read_skill", "Load a skill's full instructions before doing a task it covers.",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            _read, read_only=True, max_output=40_000)
