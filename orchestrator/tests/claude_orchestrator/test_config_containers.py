from pathlib import Path

import yaml

from claude_orchestrator.config import load_config


def _load(tmp_path: Path, **extra: object) -> tuple[str, ...]:
    (tmp_path / "main").mkdir()
    raw = {
        "whatsapp": {"allowed_sender_jid": "me"},
        "projects": [{"name": "main", "path": str(tmp_path / "main")}],
        **extra,
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path).project_containers


def test_containers_default_to_empty(tmp_path: Path) -> None:
    """No project_containers key means no containers."""
    assert _load(tmp_path, project_root=str(tmp_path)) == ()


def test_containers_are_normalized(tmp_path: Path) -> None:
    """Trailing slashes are dropped from container names."""
    assert _load(tmp_path, project_root=str(tmp_path), project_containers=["c", "c/car/"]) == (
        "c",
        "c/car",
    )


def test_invalid_containers_are_skipped(tmp_path: Path) -> None:
    """Absolute, traversal, hidden and empty entries are skipped."""
    entries = ["/abs", "../up", "c/..", ".hidden", "c//x", "", "ok"]
    assert _load(tmp_path, project_root=str(tmp_path), project_containers=entries) == ("ok",)


def test_containers_need_project_root(tmp_path: Path) -> None:
    """project_containers is ignored when project_root is unset."""
    assert _load(tmp_path, project_containers=["c"]) == ()
