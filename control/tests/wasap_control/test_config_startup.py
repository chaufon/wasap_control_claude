import os
from pathlib import Path

import pytest
import yaml

from wasap_control import main as main_module
from wasap_control.config import (
    ClaudeCodeConfig,
    Config,
    RemoteControlConfig,
    WhatsAppConfig,
    load_config,
)
from wasap_control.main import check_runtime, load_config_or_exit


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
    monkeypatch.setattr(main_module.os.path, "isfile", lambda path: str(path) in executable)
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


def test_check_runtime_rejects_a_directory_as_the_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute claude_code.binary that is a directory is not a usable binary."""
    monkeypatch.setattr(main_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    directory = tmp_path / "claude"
    directory.mkdir(mode=0o755)
    problems = check_runtime(_config(binary=str(directory)))
    assert problems == [f"claude_code.binary {str(directory)!r} is not an executable file or on PATH"]


def test_check_runtime_accepts_an_executable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute claude_code.binary that is an executable regular file passes."""
    monkeypatch.setattr(main_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert check_runtime(_config(binary=str(binary))) == []


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "config file not found"),
        ("projects: []\nproject_root: /tmp\n", "allowed_sender_jid is required"),
        ("whatsapp: [unclosed\n", "configuration error"),
        ("whatsapp: just-a-string\n", "configuration error"),
    ],
    ids=["missing-file", "missing-sender", "invalid-yaml", "wrong-shape"],
)
def test_configuration_errors_exit_78(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, content: str | None, message: str
) -> None:
    """A missing, incomplete or malformed config exits 78 with one clear log line."""
    config_path = tmp_path / "config.yaml"
    if content is not None:
        config_path.write_text(content)
    with pytest.raises(SystemExit) as exited:
        load_config_or_exit(config_path)
    assert exited.value.code == 78
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert errors[0].startswith("configuration error: ")
    assert message in errors[0]


def test_unreadable_config_exits_78(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A config path that cannot be read as a file exits 78."""
    config_path = tmp_path / "config.yaml"
    config_path.mkdir()
    with pytest.raises(SystemExit) as exited:
        load_config_or_exit(config_path)
    assert exited.value.code == 78
    assert "configuration error" in caplog.text


def test_a_valid_config_loads(tmp_path: Path) -> None:
    """load_config_or_exit returns the config when it is valid."""
    config = load_config_or_exit(_write(tmp_path, projects=[], project_root=str(tmp_path)))
    assert config.whatsapp.allowed_sender_jid == "me"
