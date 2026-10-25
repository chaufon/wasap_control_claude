from pathlib import Path

from wasap_control.projects import render_listing, scan_project_root, sort_key


def _make(root: Path, *names: str) -> None:
    for name in names:
        (root / name).mkdir(parents=True)


def _by_name(name: str, path: Path) -> str:
    return name


def test_no_root_returns_nothing() -> None:
    """Without a project_root there is nothing to scan."""
    scan = scan_project_root(None, ("c",))
    assert scan.projects == {}
    assert scan.containers == {}


def test_flat_scan_without_containers(tmp_path: Path) -> None:
    """Every non-hidden folder directly under the root is a project; files are ignored."""
    _make(tmp_path, "alpha", "beta", ".hidden")
    (tmp_path / "file.txt").write_text("x")
    scan = scan_project_root(tmp_path, ())
    assert scan.projects == {"alpha": tmp_path / "alpha", "beta": tmp_path / "beta"}
    assert scan.containers == {}


def test_container_children_are_projects_and_container_is_not(tmp_path: Path) -> None:
    """A container is not a project; its non-hidden children are, named by relative path."""
    _make(tmp_path, "alpha", "c/whey", "c/fitnotes", "c/.idea")
    scan = scan_project_root(tmp_path, ("c",))
    assert scan.projects == {
        "alpha": tmp_path / "alpha",
        "c/fitnotes": tmp_path / "c" / "fitnotes",
        "c/whey": tmp_path / "c" / "whey",
    }
    assert scan.containers == {"c": tmp_path / "c"}


def test_nested_container_three_levels(tmp_path: Path) -> None:
    """A container inside a container gives three-level project names."""
    _make(tmp_path, "c/car/maintenance", "c/car/crash26", "c/whey")
    scan = scan_project_root(tmp_path, ("c", "c/car"))
    assert set(scan.projects) == {"c/car/maintenance", "c/car/crash26", "c/whey"}
    assert set(scan.containers) == {"c", "c/car"}


def test_container_whose_parent_is_not_a_container_is_not_scanned(tmp_path: Path) -> None:
    """A container is only reached through the root or another container."""
    _make(tmp_path, "c/car/maintenance")
    scan = scan_project_root(tmp_path, ("c/car",))
    assert set(scan.projects) == {"c"}
    assert scan.containers == {}


def test_missing_container_is_skipped(tmp_path: Path) -> None:
    """A configured container with no folder is left out."""
    _make(tmp_path, "alpha")
    scan = scan_project_root(tmp_path, ("gone",))
    assert set(scan.projects) == {"alpha"}
    assert scan.containers == {}


def test_sort_key_puts_children_right_after_their_container() -> None:
    """Sorting by path parts keeps a container's children together."""
    names = ["c3app", "c/whey", "chat", "c/car/x", "b"]
    assert sorted(names, key=sort_key) == ["b", "c/car/x", "c/whey", "c3app", "chat"]


def _listing(tmp_path: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    _make(tmp_path, "chaufon", "c/fitnotes", "c/whey", "c/car/crash26", "c/car/maintenance", "zeta")
    scan = scan_project_root(tmp_path, ("c", "c/car"))
    projects = dict(sorted(scan.projects.items(), key=lambda item: sort_key(item[0])))
    return projects, scan.containers


def test_render_full_listing(tmp_path: Path) -> None:
    """Containers are unnumbered headers; children are indented under them."""
    projects, containers = _listing(tmp_path)
    assert render_listing(projects, containers, _by_name) == [
        "📁 c",
        "  📁 c/car",
        "    1. c/car/crash26",
        "    2. c/car/maintenance",
        "  3. c/fitnotes",
        "  4. c/whey",
        "5. chaufon",
        "6. zeta",
    ]


def test_render_subtree_keeps_global_numbers(tmp_path: Path) -> None:
    """A container's subtree uses the same numbers as the full listing."""
    projects, containers = _listing(tmp_path)
    assert render_listing(projects, containers, _by_name, under="c/car") == [
        "1. c/car/crash26",
        "2. c/car/maintenance",
    ]
    assert render_listing(projects, containers, _by_name, under="c") == [
        "📁 c/car",
        "  1. c/car/crash26",
        "  2. c/car/maintenance",
        "3. c/fitnotes",
        "4. c/whey",
    ]
