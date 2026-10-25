"""Config writer used by install.sh.

Run as `python -m wasap_control.install_config <command>`:

  write       create a config file from deploy/config.example.yaml
  set-sender  set whatsapp.allowed_sender_jid in an existing file
  get-sender  print the configured sender JID, if any
  diff        print the keys missing from, or obsolete in, an existing file

The files are rewritten with yaml.safe_dump, so comments are not preserved.
"""

import argparse
import contextlib
import copy
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml

# The repo layout: <repo>/control/src/wasap_control/install_config.py.
DEFAULT_EXAMPLE_PATH = Path(__file__).resolve().parents[3] / "deploy" / "config.example.yaml"
PLACEHOLDER_JID = "REPLACE_ME@s.whatsapp.net"
WHISPER_BY_DEVICE = {
    "cuda": {"model": "large-v3", "device": "cuda"},
    "cpu": {"model": "medium", "device": "cpu"},
}
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan")


def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a YAML mapping")
    return data


def _save_yaml(path: Path, data: dict) -> None:
    """Write `data` to `path` with mode 0600, creating parent directories.

    The text goes to a temp file in the same directory, is fsynced, and
    replaces `path` in one rename, so a crash leaves either the old or the
    new file, never a partial one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    # mkstemp creates the file with mode 0600.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Flush a rename in `directory` to disk, where the platform allows it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _dotted_keys(mapping: dict, prefix: str = "") -> set[str]:
    """Dotted names of every key in nested mappings; lists are not entered."""
    keys: set[str] = set()
    for key, value in mapping.items():
        name = f"{prefix}{key}"
        keys.add(name)
        if isinstance(value, dict):
            keys |= _dotted_keys(value, f"{name}.")
    return keys


def _error(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _cmd_write(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists():
        return _error(f"{path} already exists; not overwriting it")
    data = copy.deepcopy(_load_yaml(Path(args.example)))
    data["project_root"] = args.project_root
    data["projects"] = []
    data.setdefault("claude_code", {})["binary"] = args.claude_binary
    data.setdefault("remote_control", {})["permission_mode"] = args.permission_mode
    data.setdefault("whisper", {}).update(WHISPER_BY_DEVICE[args.whisper_device])
    _save_yaml(path, data)
    return 0


def _cmd_set_sender(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        return _error(f"{path} does not exist")
    data = _load_yaml(path)
    whatsapp = data.get("whatsapp")
    if not isinstance(whatsapp, dict):
        whatsapp = data["whatsapp"] = {}
    whatsapp["allowed_sender_jid"] = args.jid
    _save_yaml(path, data)
    return 0


def _cmd_get_sender(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        return _error(f"{path} does not exist")
    whatsapp = _load_yaml(path).get("whatsapp")
    jid = whatsapp.get("allowed_sender_jid") if isinstance(whatsapp, dict) else None
    if jid and jid != PLACEHOLDER_JID:
        print(jid)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        return _error(f"{path} does not exist")
    wanted = _dotted_keys(_load_yaml(Path(args.example)))
    present = _dotted_keys(_load_yaml(path))
    for key in sorted(wanted - present):
        print(f"missing: {key}")
    for key in sorted(present - wanted):
        print(f"obsolete: {key}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wasap_control.install_config")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, *, example: bool) -> argparse.ArgumentParser:
        p = sub.add_parser(name)
        p.add_argument("--path", required=True)
        if example:
            p.add_argument("--example", default=str(DEFAULT_EXAMPLE_PATH))
        p.set_defaults(handler=handler)
        return p

    write = add("write", _cmd_write, example=True)
    write.add_argument("--project-root", required=True)
    write.add_argument("--claude-binary", required=True)
    write.add_argument("--whisper-device", required=True, choices=sorted(WHISPER_BY_DEVICE))
    write.add_argument("--permission-mode", required=True, choices=PERMISSION_MODES)
    add("set-sender", _cmd_set_sender, example=False).add_argument("--jid", required=True)
    add("get-sender", _cmd_get_sender, example=False)
    add("diff", _cmd_diff, example=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
