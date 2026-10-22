"""Project discovery under project_root and its containers (SPEC.md §7.2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProjectScan:
    """Projects (name -> path) and present containers (name -> path) found
    under project_root. Names are paths relative to project_root."""

    projects: dict[str, Path] = field(default_factory=dict)
    containers: dict[str, Path] = field(default_factory=dict)


def scan_project_root(root: Path | None, containers: tuple[str, ...]) -> ProjectScan:
    """Every non-hidden folder directly under `root` is a project, unless its
    relative name is a listed container. A container is not a project: it is
    scanned one level deeper with the same rule. A container is only reached
    through `root` or another container, so a listed container whose parent
    is neither, or whose folder is missing, is never scanned."""
    if root is None or not root.is_dir():
        return ProjectScan()
    container_names = set(containers)
    scan = ProjectScan()

    def visit(folder: Path, prefix: str) -> None:
        for entry in sorted(folder.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            name = f"{prefix}{entry.name}"
            if name in container_names:
                scan.containers[name] = entry
                visit(entry, f"{name}/")
            else:
                scan.projects[name] = entry

    visit(root, "")
    return scan


def sort_key(name: str) -> list[str]:
    """Sort by path parts, so a container's children come right after it
    ("c/whey" before "c3app") in /projects."""
    return name.split("/")


def render_listing(
    projects: dict[str, Path],
    containers: dict[str, Path],
    format_line: Callable[[str, Path], str],
    under: str | None = None,
) -> list[str]:
    """/projects lines: a numbered line per project and an unnumbered "📁"
    header per container, indented two spaces per nesting level. Numbers
    follow the order of `projects`. With `under`, only that container's
    subtree is rendered, indented relative to it, keeping the full listing's
    numbers so /project <number> still works."""
    numbers = {name: i for i, name in enumerate(projects, start=1)}
    entries = [(sort_key(name), 0, name) for name in containers]
    entries += [(sort_key(name), 1, name) for name in projects]
    base_depth = 0
    if under is not None:
        prefix = f"{under}/"
        entries = [entry for entry in entries if entry[2].startswith(prefix)]
        base_depth = under.count("/") + 1

    lines = []
    for _, kind, name in sorted(entries):
        indent = "  " * (name.count("/") - base_depth)
        if kind == 0:
            lines.append(f"{indent}📁 {name}")
        else:
            lines.append(f"{indent}{numbers[name]}. {format_line(name, projects[name])}")
    return lines
