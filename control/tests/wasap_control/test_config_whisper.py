from pathlib import Path

import yaml

from wasap_control.config import load_config


def test_whisper_defaults_are_cpu_medium(tmp_path: Path) -> None:
    """A config without a whisper section uses the CPU medium model."""
    (tmp_path / "main").mkdir()
    raw = {
        "whatsapp": {"allowed_sender_jid": "me"},
        "projects": [{"name": "main", "path": str(tmp_path / "main")}],
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    whisper = load_config(config_path).whisper

    assert whisper.model == "medium"
    assert whisper.device == "cpu"
    assert whisper.fallback_device == "cpu"
    assert whisper.fallback_model == "medium"
