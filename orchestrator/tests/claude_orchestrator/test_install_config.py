"""Tests for the config writer the installer calls."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from claude_orchestrator import install_config
from claude_orchestrator.config import load_config
from claude_orchestrator.install_config import DEFAULT_EXAMPLE_PATH, main

PLACEHOLDER = "REPLACE_ME@s.whatsapp.net"


def _write(path: Path, *, device: str = "cpu", project_root: str = "~/projects") -> int:
    return main(
        [
            "write",
            "--path", str(path),
            "--project-root", project_root,
            "--claude-binary", "/opt/bin/claude",
            "--whisper-device", device,
            "--permission-mode", "auto",
        ]
    )


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def test_write_cpu_values(tmp_path):
    path = tmp_path / "nested" / "config.yaml"
    assert _write(path) == 0
    data = _load(path)
    assert data["whisper"]["model"] == "medium"
    assert data["whisper"]["device"] == "cpu"
    assert data["project_root"] == "~/projects"
    assert data["projects"] == []
    assert data["claude_code"]["binary"] == "/opt/bin/claude"
    assert data["remote_control"]["permission_mode"] == "auto"
    assert data["whatsapp"]["allowed_sender_jid"] == PLACEHOLDER
    assert "redis" not in data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_cuda_values(tmp_path):
    path = tmp_path / "config.yaml"
    assert _write(path, device="cuda", project_root="/srv/code") == 0
    data = _load(path)
    assert data["whisper"]["model"] == "large-v3"
    assert data["whisper"]["device"] == "cuda"
    assert data["project_root"] == "/srv/code"


def test_write_refuses_existing_file(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("keep: me\n")
    assert _write(path) == 1
    assert path.read_text() == "keep: me\n"
    assert str(path) in capsys.readouterr().err


def test_set_sender_keeps_other_keys(tmp_path):
    path = tmp_path / "config.yaml"
    _write(path, device="cuda")
    before = _load(path)
    assert main(["set-sender", "--path", str(path), "--jid", "51987654321@s.whatsapp.net"]) == 0
    after = _load(path)
    assert after["whatsapp"]["allowed_sender_jid"] == "51987654321@s.whatsapp.net"
    before["whatsapp"]["allowed_sender_jid"] = after["whatsapp"]["allowed_sender_jid"]
    assert after == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_set_sender_replaces_the_file_atomically(tmp_path: Path) -> None:
    """set-sender writes a new file and renames it over the old one, mode 0600,
    leaving no temp file behind."""
    path = tmp_path / "config.yaml"
    _write(path)
    path.chmod(0o644)
    old_inode = path.stat().st_ino
    assert main(["set-sender", "--path", str(path), "--jid", "519@s.whatsapp.net"]) == 0
    assert path.stat().st_ino != old_inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml"]


def test_set_sender_keeps_the_old_file_when_the_rename_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write leaves the old file intact and removes the temp file."""
    path = tmp_path / "config.yaml"
    _write(path)
    before = path.read_text()

    def failing_replace(src: str, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(install_config.os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        main(["set-sender", "--path", str(path), "--jid", "519@s.whatsapp.net"])
    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.yaml"]


def test_diff_reports_missing_and_obsolete(tmp_path, capsys):
    example = tmp_path / "example.yaml"
    example.write_text("a:\n  b: 1\n  c: 2\nlist: []\ntop: 1\n")
    user = tmp_path / "user.yaml"
    user.write_text("a:\n  b: 1\n  old: 3\nlist:\n  - x: 1\nredis:\n  host: h\n")
    assert main(["diff", "--path", str(user), "--example", str(example)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "missing: a.c",
        "missing: top",
        "obsolete: a.old",
        "obsolete: redis",
        "obsolete: redis.host",
    ]


def test_diff_prints_nothing_when_equal(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _write(path)
    assert main(["diff", "--path", str(path)]) == 0
    assert capsys.readouterr().out == ""


def test_get_sender_ignores_placeholder(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _write(path)
    assert main(["get-sender", "--path", str(path)]) == 0
    assert capsys.readouterr().out == ""
    main(["set-sender", "--path", str(path), "--jid", "519@s.whatsapp.net"])
    assert main(["get-sender", "--path", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "519@s.whatsapp.net"


def test_get_sender_missing_file_exits_1(tmp_path, capsys):
    assert main(["get-sender", "--path", str(tmp_path / "nope.yaml")]) == 1
    assert capsys.readouterr().err


def test_example_loads_with_load_config(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    path = tmp_path / "config.yaml"
    assert _write(path, project_root=str(projects)) == 0
    main(["set-sender", "--path", str(path), "--jid", "519@s.whatsapp.net"])
    config = load_config(path)
    assert config.whatsapp.allowed_sender_jid == "519@s.whatsapp.net"
    assert config.project_root == projects
    assert config.projects == []
    assert config.claude_code.binary == "/opt/bin/claude"


def test_example_has_no_machine_paths_or_spec_refs():
    text = DEFAULT_EXAMPLE_PATH.read_text()
    assert "/home/" not in text
    assert "SPEC.md" not in text


def test_module_entry_point(tmp_path):
    path = tmp_path / "config.yaml"
    result = subprocess.run(
        [
            sys.executable, "-m", "claude_orchestrator.install_config", "write",
            "--path", str(path), "--project-root", "~/projects",
            "--claude-binary", "claude", "--whisper-device", "cpu",
            "--permission-mode", "default",
        ],
        capture_output=True, text=True, env=os.environ,
    )
    assert result.returncode == 0, result.stderr
    assert _load(path)["remote_control"]["permission_mode"] == "default"
