import os
from pathlib import Path

import pytest
import yaml

from claude_orchestrator import main as main_module
from claude_orchestrator.config import (
    ClaudeCodeConfig,
    Config,
    RemoteControlConfig,
    WhatsAppConfig,
    load_config,
)
from claude_orchestrator.main import check_runtime


def _write(tmp_path: Path, **extra: object) -> Path:
    raw = {"whatsapp": {"allowed_sender_jid": "me"}, **extra}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def test_empty_projects_with_project_root_loads(tmp_path: Path) -> None:
    """A fresh install has no static projects and a project_root."""
    config = load_config(_write(tmp_path, projects=[], project_root=str(tmp_path)))
    assert config.projects == []


def test_empty_projects_without_project_root_raises(tmp_path: Path) -> None:
    """Without projects and without project_root there is nothing to work in."""
    with pytest.raises(ValueError, match="project"):
        load_config(_write(tmp_path, projects=[]))


def test_default_project_falls_back_to_project_root_folder(tmp_path: Path) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "alpha").mkdir()
    config = load_config(_write(tmp_path, projects=[], project_root=str(tmp_path)))
    default = config.default_project
    assert default is not None
    assert (default.name, default.path) == ("alpha", tmp_path / "alpha")


def test_default_project_prefers_the_first_static_project(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "main").mkdir()
    config = load_config(
        _write(
            tmp_path,
            projects=[{"name": "main", "path": str(tmp_path / "main")}],
            project_root=str(tmp_path),
        )
    )
    assert config.default_project is not None
    assert config.default_project.name == "main"


def test_default_project_none_when_nothing_found(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, projects=[], project_root=str(tmp_path)))
    assert config.default_project is None


def _config(
    binary: str = "/usr/bin/claude", permission_mode: str = "auto"
) -> Config:
    return Config(
        whatsapp=WhatsAppConfig(allowed_sender_jid="me"),
        claude_code=ClaudeCodeConfig(binary=binary),
        remote_control=RemoteControlConfig(permission_mode=permission_mode),
    )


def _fake_system(
    monkeypatch: pytest.MonkeyPatch, on_path: set[str], executable: set[str], euid: int = 1000
) -> None:
    monkeypatch.setattr(
        main_module.shutil, "which", lambda name: f"/bin/{name}" if name in on_path else None
    )
    monkeypatch.setattr(main_module.os, "access", lambda path, mode: str(path) in executable)
    monkeypatch.setattr(os, "geteuid", lambda: euid)


def test_check_runtime_passes_when_everything_is_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(monkeypatch, {"tmux", "ffmpeg"}, {"/usr/bin/claude"})
    assert check_runtime(_config()) == []


def test_check_runtime_finds_a_bare_binary_name_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(monkeypatch, {"claude", "tmux", "ffmpeg"}, set())
    assert check_runtime(_config(binary="claude")) == []


def test_check_runtime_reports_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(monkeypatch, set(), set())
    problems = check_runtime(_config())
    assert len(problems) == 3
    assert any("/usr/bin/claude" in p for p in problems)
    assert any("tmux" in p for p in problems)
    assert any("ffmpeg" in p for p in problems)


def test_check_runtime_rejects_bypass_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(monkeypatch, {"tmux", "ffmpeg"}, {"/usr/bin/claude"}, euid=0)
    problems = check_runtime(_config(permission_mode="bypassPermissions"))
    assert problems == ["permission_mode bypassPermissions cannot run as root; use auto"]


def test_check_runtime_allows_auto_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(monkeypatch, {"tmux", "ffmpeg"}, {"/usr/bin/claude"}, euid=0)
    assert check_runtime(_config(permission_mode="auto")) == []


def test_bypass_as_non_root_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_system(monkeypatch, {"tmux", "ffmpeg"}, {"/usr/bin/claude"}, euid=1000)
    assert check_runtime(_config(permission_mode="bypassPermissions")) == []
