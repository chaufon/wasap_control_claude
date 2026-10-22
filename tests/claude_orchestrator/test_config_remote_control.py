from pathlib import Path

import pytest
import yaml

from claude_orchestrator.config import RemoteControlConfig, load_config


def _load(tmp_path: Path, **extra: object) -> RemoteControlConfig:
    (tmp_path / "main").mkdir()
    raw = {
        "whatsapp": {"allowed_sender_jid": "me"},
        "projects": [{"name": "main", "path": str(tmp_path / "main")}],
        **extra,
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path).remote_control


def test_defaults(tmp_path: Path) -> None:
    """No remote_control section means no permission prompts, in the project folder itself."""
    assert _load(tmp_path) == RemoteControlConfig(permission_mode="bypassPermissions", spawn="same-dir", chrome=False)


def test_values_are_read(tmp_path: Path) -> None:
    """Every value comes from the remote_control section."""
    config = _load(tmp_path, remote_control={"permission_mode": "plan", "spawn": "worktree", "chrome": True})
    assert config == RemoteControlConfig(permission_mode="plan", spawn="worktree", chrome=True)


@pytest.mark.parametrize("section", [{"permission_mode": "yolo"}, {"spawn": "session"}, {"chrome": "yes"}])
def test_invalid_values_are_rejected(tmp_path: Path, section: dict[str, str]) -> None:
    """An unknown permission mode, spawn mode or non-boolean chrome fails at startup."""
    with pytest.raises(ValueError, match="config.remote_control"):
        _load(tmp_path, remote_control=section)
