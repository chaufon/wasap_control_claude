"""Tests for install.sh and deploy/install/systemd.sh.

Every test runs install.sh from a temp copy of the repo with a temp HOME and a
PATH made only of fake binaries plus an allowlist of basic utilities, so no
real systemctl, loginctl, sudo, node, npm, uv or claude can be reached.
"""

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASH = shutil.which("bash")
REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "deploy" / "systemd"
SENDER = "15550001@s.whatsapp.net"
UTILITIES = [
    "sh", "bash", "env", "cat", "mkdir", "rm", "cp", "mv", "grep", "sed",
    "awk", "mktemp", "dirname", "basename", "uname", "tee", "chmod", "cut",
    "tail", "head", "id", "tr", "sort", "ls", "touch", "readlink",
]

FAKES = {
    "systemctl": """
log systemctl "$@"
for a in "$@"; do
    [ "$a" = is-active ] && exit "${FAKE_ACTIVE_STATUS:-3}"
done
exit 0
""",
    "loginctl": """
log loginctl "$@"
exit "${FAKE_LOGINCTL_STATUS:-0}"
""",
    "node": """
log node "$@"
[ "$1" = --version ] && echo "${FAKE_NODE_VERSION:-v22.1.0}"
for a in "$@"; do
    if [ "$a" = --pair ]; then
        echo "QR code here" >&2
        echo "PAIRED $FAKE_SENDER"
    fi
done
exit 0
""",
    "npm": """
log npm "$@"
exit 0
""",
    "uv": """
log uv "$@"
if [ "$1" = sync ]; then
    proj=""
    prev=""
    for a in "$@"; do
        [ "$prev" = --project ] && proj="$a"
        prev="$a"
    done
    mkdir -p "$proj/.venv/bin"
    cp "$FAKE_STATE/venv-python" "$proj/.venv/bin/python"
    chmod 755 "$proj/.venv/bin/python"
fi
exit 0
""",
    "claude": """
log claude "$@"
if [ "$1 $2" = "auth status" ]; then
    if [ -e "$FAKE_STATE/claude-logged-in" ]; then
        echo '{"loggedIn": true}'
    else
        echo '{"loggedIn": false}'
    fi
fi
if [ "$1 $2" = "auth login" ] && [ -n "${FAKE_LOGIN_WORKS:-}" ]; then
    touch "$FAKE_STATE/claude-logged-in"
fi
exit 0
""",
    "ffmpeg": "log ffmpeg \"$@\"",
    "tmux": "log tmux \"$@\"",
    "curl": "log curl \"$@\"\nexit 1",
    "nvidia-smi": "log nvidia-smi \"$@\"\nexit \"${FAKE_GPU_STATUS:-1}\"",
    "getent": 'echo "$2:x:1000:1000::$HOME:/bin/bash"',
    "sudo": """
log sudo "$@"
while [ $# -gt 0 ]; do
    case "$1" in
        -u) shift 2 ;;
        -H) shift ;;
        *) break ;;
    esac
done
exec "$@"
""",
}


class Env:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.tmpdir = tmp_path / "tmp"
        self.state = tmp_path / "state"
        self.bin = tmp_path / "bin"
        self.sysbin = tmp_path / "sysbin"
        self.log = tmp_path / "calls.log"
        for d in (self.home, self.tmpdir, self.state, self.bin, self.sysbin):
            d.mkdir()
        self._copy_repo()
        self._make_bins()
        (self.state / "claude-logged-in").touch()
        (self.state / "venv-python").write_text(
            "#!/bin/sh\n"
            f'PYTHONPATH="{self.repo / "orchestrator" / "src"}" '
            f'exec "{sys.executable}" "$@"\n'
        )
        self.extra_env = {}

    def _copy_repo(self):
        ignore = shutil.ignore_patterns("__pycache__", "tests")
        self.repo.mkdir()
        if (REPO / "install.sh").exists():
            shutil.copy2(REPO / "install.sh", self.repo / "install.sh")
        shutil.copytree(REPO / "deploy", self.repo / "deploy", ignore=ignore)
        (self.repo / "orchestrator").mkdir()
        shutil.copy2(REPO / "orchestrator" / "pyproject.toml", self.repo / "orchestrator")
        shutil.copytree(
            REPO / "orchestrator" / "src", self.repo / "orchestrator" / "src", ignore=ignore
        )
        (self.repo / "bridge").mkdir()
        shutil.copy2(REPO / "bridge" / "package.json", self.repo / "bridge")

    def _make_bins(self):
        for name in UTILITIES:
            real = shutil.which(name)
            assert real, name
            (self.sysbin / name).symlink_to(real)
        for name, body in FAKES.items():
            self.add_fake(name, body)

    def add_fake(self, name, body):
        path = self.bin / name
        path.write_text(
            "#!/bin/sh\n"
            'log() { printf "%s\\n" "$*" >> "$FAKE_LOG"; }\n' + body.strip() + "\n"
        )
        path.chmod(0o755)

    def remove_fake(self, name):
        (self.bin / name).unlink()

    def make_venv(self):
        venv_bin = self.repo / "orchestrator" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        shutil.copy2(self.state / "venv-python", venv_bin / "python")
        (venv_bin / "python").chmod(0o755)

    @property
    def config(self):
        return self.home / ".config" / "claude-orchestrator" / "config.yaml"

    @property
    def unit_dir(self):
        return self.home / ".config" / "systemd" / "user"

    def write_config(self, sender=SENDER):
        if not (self.repo / "orchestrator" / ".venv").exists():
            self.make_venv()
        py = str(self.repo / "orchestrator" / ".venv" / "bin" / "python")
        base = [py, "-m", "claude_orchestrator.install_config"]
        subprocess.run(
            base + [
                "write", "--path", str(self.config), "--project-root", "~/projects",
                "--claude-binary", str(self.bin / "claude"), "--whisper-device", "cpu",
                "--permission-mode", "bypassPermissions",
            ],
            check=True,
        )
        if sender:
            subprocess.run(
                base + ["set-sender", "--path", str(self.config), "--jid", sender],
                check=True,
            )

    def env(self, euid="1000"):
        env = {
            "PATH": f"{self.bin}:{self.sysbin}",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "FAKE_LOG": str(self.log),
            "FAKE_STATE": str(self.state),
            "FAKE_SENDER": SENDER,
            "OS_RELEASE_FILE": str(REPO / "deploy" / "tests" / "fixtures" / "os-release" / "ubuntu"),
            "CURRENT_EUID": euid,
            "LANG": "C",
        }
        env.update(self.extra_env)
        return env

    def run(self, *args, euid="1000", stdin=""):
        return subprocess.run(
            [BASH, str(self.repo / "install.sh"), *args],
            cwd=self.repo,
            env=self.env(euid),
            input=stdin,
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=120,
        )

    def calls(self, name=None):
        if not self.log.exists():
            return []
        lines = self.log.read_text().splitlines()
        if name is None:
            return lines
        return [line for line in lines if line.split(" ", 1)[0] == name]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def files_under(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def render(template, repo, node, environment):
    script = (
        f'source "{REPO / "deploy" / "install" / "systemd.sh"}"\n'
        'render_unit "$1" "$2" "$3" "$4"\n'
    )
    return subprocess.run(
        [BASH, "-c", script, "bash", str(template), repo, node, environment],
        capture_output=True, text=True, check=True,
    ).stdout


# --- render_unit -----------------------------------------------------------


@pytest.mark.parametrize("environment", ["Environment=LD_LIBRARY_PATH=/a/lib:/b/lib", ""])
@pytest.mark.parametrize("name", ["claude-orchestrator.service.in", "whatsapp-bridge.service.in"])
def test_render_unit_substitutes_all_placeholders(name, environment):
    text = render(TEMPLATES / name, "/srv/my repo", "/opt/node/bin/node", environment)
    assert "@" not in text
    assert "RestartPreventExitStatus=78" in text
    if name.startswith("claude-orchestrator"):
        assert "ExecStart=/srv/my repo/orchestrator/.venv/bin/claude-orchestrator" in text
        if environment:
            assert environment + "\n" in text
        else:
            assert "LD_LIBRARY_PATH" not in text
            assert "\n\nExecStart" not in text
    else:
        assert "ExecStart=/opt/node/bin/node dist/index.js" in text
        assert "WorkingDirectory=/srv/my repo/bridge" in text


# --- flags -----------------------------------------------------------------


def test_bad_flag_exits_2(env):
    result = env.run("--frobnicate")
    assert result.returncode == 2
    assert "--frobnicate" in result.stderr
    assert env.calls() == []


def test_positional_argument_exits_2(env):
    assert env.run("extra").returncode == 2


def test_check_with_uninstall_exits_2(env):
    assert env.run("--check", "--uninstall").returncode == 2


def test_help_exits_0_and_lists_flags(env):
    result = env.run("--help")
    assert result.returncode == 0
    for flag in ("--check", "--login-only", "--uninstall", "--purge", "--help"):
        assert flag in result.stdout
    assert env.calls() == []


def test_macos_exits_4(env):
    env.add_fake("uname", 'echo Darwin')
    result = env.run()
    assert result.returncode == 4
    assert "macOS support is not available yet" in result.stdout + result.stderr


# --- system packages -------------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--check"], ["--login-only"], ["--check", "--login-only"]])
def test_missing_package_without_root_exits_3_and_prints_sudo_command(env, flags):
    env.remove_fake("ffmpeg")
    result = env.run(*flags)
    assert result.returncode == 3
    expected = " ".join(["sudo ./install.sh", *flags]).strip()
    output_lines = (result.stdout + result.stderr).splitlines()
    assert any(line.strip() == expected for line in output_lines), output_lines
    assert env.calls("uv") == [] and env.calls("systemctl") == []


def test_missing_package_as_root_runs_install_command(env):
    shutil.copy2(env.bin / "ffmpeg", env.state / "ffmpeg")
    env.remove_fake("ffmpeg")
    env.add_fake("apt-get", 'log apt-get "$@"\ncp "$FAKE_STATE/ffmpeg" "$(dirname "$0")/ffmpeg"')
    env.make_venv()
    env.write_config()
    env.extra_env["SUDO_USER"] = "alice"
    result = env.run(euid="0")
    assert result.returncode == 0, result.stderr
    assert "apt-get install -y ffmpeg tmux curl" in env.calls("apt-get")


def test_unsupported_distro_exits_4_and_lists_packages(env):
    env.remove_fake("ffmpeg")
    env.extra_env["OS_RELEASE_FILE"] = str(
        REPO / "deploy" / "tests" / "fixtures" / "os-release" / "gentoo"
    )
    result = env.run(euid="0")
    assert result.returncode == 4
    assert "ffmpeg tmux curl" in result.stdout + result.stderr


# --- --check ---------------------------------------------------------------


def test_check_writes_nothing_outside_tmp(env):
    env.make_venv()
    before = files_under(env.home)
    result = env.run("--check")
    assert result.returncode == 0, result.stderr
    assert files_under(env.home) == before
    assert not (env.home / ".config").exists()
    assert env.calls("systemctl") == []
    assert env.calls("loginctl") == []
    assert env.calls("npm") == []
    assert not [c for c in env.calls("uv") if c.startswith("uv sync")]
    assert not [c for c in env.calls("node") if "--pair" in c]
    printed = [
        line.split()[-1] for line in result.stdout.splitlines()
        if line.startswith(("Config: ", "Unit: "))
    ]
    names = {Path(p).name for p in printed}
    assert {"config.yaml", "claude-orchestrator.service", "whatsapp-bridge.service"} <= names
    for p in printed:
        assert Path(p).is_file() and Path(p).is_relative_to(env.tmpdir)
        assert "@" not in Path(p).read_text() or Path(p).name == "config.yaml"


def test_check_without_venv_still_renders_units(env):
    result = env.run("--check")
    assert result.returncode == 0, result.stderr
    assert not (env.repo / "orchestrator" / ".venv").exists()
    assert "whatsapp-bridge.service" in result.stdout
    assert not (env.home / ".config").exists()


def test_check_reports_login_without_running_it(env):
    (env.state / "claude-logged-in").unlink()
    env.make_venv()
    result = env.run("--check")
    assert result.returncode == 0, result.stderr
    assert not [c for c in env.calls("claude") if c.startswith("claude auth login")]
    assert "claude auth login" in result.stdout


# --- install and re-run ----------------------------------------------------


def test_fresh_install_asks_project_root_and_pairs(env):
    result = env.run(stdin="~/work\n")
    assert result.returncode == 0, result.stderr
    assert "~/projects" in result.stdout + result.stderr
    assert (env.home / "work").is_dir()
    text = env.config.read_text()
    assert "project_root: ~/work" in text
    assert f"allowed_sender_jid: {SENDER}" in text
    assert "permission_mode: bypassPermissions" in text
    assert "device: cpu" in text
    assert [c for c in env.calls("node") if c.endswith("dist/index.js --pair")]
    assert any(c.startswith("uv sync --project") and "--extra" not in c for c in env.calls("uv"))
    assert "npm ci" in env.calls("npm") and "npm run build" in env.calls("npm")
    unit = (env.unit_dir / "claude-orchestrator.service").read_text()
    assert "@" not in unit and "LD_LIBRARY_PATH" not in unit
    bridge = (env.unit_dir / "whatsapp-bridge.service").read_text()
    assert f"ExecStart={env.bin / 'node'} dist/index.js" in bridge
    assert "loginctl enable-linger" in " ".join(env.calls("loginctl"))


def test_fresh_install_default_project_root_on_empty_answer(env):
    result = env.run(stdin="\n")
    assert result.returncode == 0, result.stderr
    assert "project_root: ~/projects" in env.config.read_text()
    assert (env.home / "projects").is_dir()


def test_rerun_keeps_config_and_skips_pairing(env):
    env.make_venv()
    env.write_config()
    before = env.config.read_bytes()
    env.unit_dir.mkdir(parents=True)
    (env.unit_dir / "whatsapp-bridge.service").write_text("stale\n")
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert env.config.read_bytes() == before
    assert not [c for c in env.calls("node") if "--pair" in c]
    systemctl = env.calls("systemctl")
    assert any(" restart " in f" {c} " for c in systemctl), systemctl
    assert "systemctl --user daemon-reload" in systemctl
    assert (env.unit_dir / "whatsapp-bridge.service").read_text() != "stale\n"
    assert "@" not in (env.unit_dir / "claude-orchestrator.service").read_text()


def test_pairing_stops_running_bridge_first(env):
    env.make_venv()
    env.write_config(sender=None)
    env.extra_env["FAKE_ACTIVE_STATUS"] = "0"
    result = env.run()
    assert result.returncode == 0, result.stderr
    log = env.calls()
    stop = log.index("systemctl --user stop whatsapp-bridge.service")
    pair = next(i for i, c in enumerate(log) if c.startswith("node") and "--pair" in c)
    assert stop < pair
    assert f"allowed_sender_jid: {SENDER}" in env.config.read_text()


def test_gpu_installs_cuda_extra_and_sets_library_path(env):
    env.extra_env["FAKE_GPU_STATUS"] = "0"
    result = env.run(stdin="\n")
    assert result.returncode == 0, result.stderr
    assert any("--extra cuda" in c for c in env.calls("uv") if c.startswith("uv sync"))
    assert "device: cuda" in env.config.read_text()
    unit = (env.unit_dir / "claude-orchestrator.service").read_text()
    try:
        import nvidia  # noqa: F401

        dirs = sorted(
            d for p in nvidia.__path__ for d in glob.glob(os.path.join(p, "*", "lib"))
        )
    except ImportError:
        dirs = []
    if dirs:
        assert "Environment=LD_LIBRARY_PATH=" + ":".join(dirs) + "\n" in unit
    else:
        assert "LD_LIBRARY_PATH" not in unit
    assert "@" not in unit


def test_old_system_node_uses_private_node(env):
    env.extra_env["FAKE_NODE_VERSION"] = "v18.19.0"
    private = env.home / ".local" / "share" / "claude-orchestrator" / "node" / "bin"
    private.mkdir(parents=True)
    shutil.copy2(env.bin / "node", private / "node")
    env.make_venv()
    env.write_config()
    # The private copy reports the same fake version unless told otherwise.
    (private / "node").write_text(
        (env.bin / "node").read_text().replace("${FAKE_NODE_VERSION:-v22.1.0}", "v24.0.0")
    )
    result = env.run()
    assert result.returncode == 0, result.stderr
    bridge = (env.unit_dir / "whatsapp-bridge.service").read_text()
    assert f"ExecStart={private / 'node'} dist/index.js" in bridge


def test_not_logged_in_runs_login_then_continues(env):
    (env.state / "claude-logged-in").unlink()
    env.extra_env["FAKE_LOGIN_WORKS"] = "1"
    env.make_venv()
    env.write_config()
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "claude auth login" in env.calls("claude")


def test_login_failure_exits_78(env):
    (env.state / "claude-logged-in").unlink()
    env.make_venv()
    env.write_config()
    result = env.run()
    assert result.returncode == 78
    assert env.calls("systemctl") == []


# --- linger ----------------------------------------------------------------


def test_login_only_disables_linger(env):
    env.make_venv()
    env.write_config()
    result = env.run("--login-only")
    assert result.returncode == 0, result.stderr
    loginctl = env.calls("loginctl")
    assert any(c.startswith("loginctl disable-linger ") for c in loginctl)
    assert not any("enable-linger" in c for c in loginctl)


def test_linger_failure_without_root_exits_3(env):
    env.make_venv()
    env.write_config()
    env.extra_env["FAKE_LOGINCTL_STATUS"] = "1"
    result = env.run()
    assert result.returncode == 3
    assert "sudo ./install.sh" in result.stdout + result.stderr


# --- root ------------------------------------------------------------------


def test_sudo_targets_invoking_user(env):
    env.make_venv()
    env.write_config()
    env.extra_env["SUDO_USER"] = "alice"
    result = env.run(euid="0")
    assert result.returncode == 0, result.stderr
    systemctl = env.calls("systemctl")
    assert systemctl and all("--user --machine=alice@" in c for c in systemctl)
    assert "loginctl enable-linger alice" in env.calls("loginctl")
    assert any(c.startswith("sudo -u alice -H") for c in env.calls("sudo"))


def test_plain_root_writes_auto_permission_mode(env):
    env.add_fake("id", 'if [ "$1" = -un ]; then echo root; else /usr/bin/id "$@"; fi')
    result = env.run(euid="0", stdin="\n")
    assert result.returncode == 0, result.stderr
    assert "permission_mode: auto" in env.config.read_text()
    assert all("--machine" not in c for c in env.calls("systemctl"))
    assert env.calls("sudo") == []


# --- uninstall -------------------------------------------------------------


def make_installed(env):
    env.make_venv()
    env.write_config()
    env.unit_dir.mkdir(parents=True)
    for name in ("claude-orchestrator.service", "whatsapp-bridge.service"):
        (env.unit_dir / name).write_text("[Unit]\n")
    for d in (
        env.home / ".local" / "share" / "whatsapp-bridge",
        env.home / ".local" / "share" / "claude-orchestrator",
    ):
        d.mkdir(parents=True)
        (d / "data").write_text("x")


def test_uninstall_removes_units_and_keeps_config(env):
    make_installed(env)
    before = env.config.read_bytes()
    result = env.run("--uninstall")
    assert result.returncode == 0, result.stderr
    assert not (env.unit_dir / "claude-orchestrator.service").exists()
    assert not (env.unit_dir / "whatsapp-bridge.service").exists()
    assert env.config.read_bytes() == before
    assert (env.home / ".local" / "share" / "whatsapp-bridge" / "data").exists()
    systemctl = env.calls("systemctl")
    assert (
        "systemctl --user disable --now whatsapp-bridge.service claude-orchestrator.service"
        in systemctl
    )
    assert systemctl[-1] == "systemctl --user daemon-reload"
    assert env.calls("loginctl") == [] and env.calls("uv") == []


def test_purge_confirmed_deletes_data(env):
    make_installed(env)
    result = env.run("--uninstall", "--purge", stdin="y\n")
    assert result.returncode == 0, result.stderr
    assert "[y/N]" in result.stdout + result.stderr
    assert not (env.home / ".config" / "claude-orchestrator").exists()
    assert not (env.home / ".local" / "share" / "whatsapp-bridge").exists()
    assert not (env.home / ".local" / "share" / "claude-orchestrator").exists()
    assert not (env.unit_dir / "whatsapp-bridge.service").exists()


@pytest.mark.parametrize("answer", ["", "n\n", "whatever\n"])
def test_purge_declined_keeps_data(env, answer):
    make_installed(env)
    result = env.run("--purge", stdin=answer)
    assert result.returncode == 0, result.stderr
    assert env.config.exists()
    assert (env.home / ".local" / "share" / "whatsapp-bridge" / "data").exists()
    assert not (env.unit_dir / "whatsapp-bridge.service").exists()


def test_install_sh_parses_under_bash_posix():
    result = subprocess.run(
        [BASH, "--posix", "-n", str(REPO / "install.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
