from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from claude_orchestrator.config import load_config

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


def _write(tmp_path: Path, **extra: object) -> Path:
    (tmp_path / "main").mkdir()
    raw = {
        "whatsapp": {"allowed_sender_jid": "me"},
        "projects": [{"name": "main", "path": str(tmp_path / "main")}],
        **extra,
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def test_socket_path_defaults_to_the_runtime_dir(tmp_path: Path, monkeypatch: "MonkeyPatch") -> None:
    """Without a transport section the socket lives in $XDG_RUNTIME_DIR."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    config = load_config(_write(tmp_path))
    assert config.transport.socket_path == Path("/run/user/1234/claude-orchestrator.sock")


def test_socket_path_is_read_and_expanded(tmp_path: Path) -> None:
    """transport.socket_path is used as given, with ~ expanded."""
    config = load_config(_write(tmp_path, transport={"socket_path": "~/orchestrator.sock"}))
    assert config.transport.socket_path == Path.home() / "orchestrator.sock"


def test_a_leftover_redis_section_is_ignored(tmp_path: Path) -> None:
    """An unrelated top-level `redis:` section does not stop the config from loading."""
    config = load_config(_write(tmp_path, redis={"host": "127.0.0.1", "port": 6379}))
    assert config.transport.socket_path.name.startswith("claude-orchestrator")
