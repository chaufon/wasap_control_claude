import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash")
DEPLOY = Path(__file__).resolve().parent.parent
LIB = DEPLOY / "install"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "os-release"
CURRENT_USER = subprocess.run(
    ["id", "-un"], capture_output=True, text=True
).stdout.strip()
LIB_FILES = ["os.sh", "packages.sh", "node.sh", "tools.sh", "user.sh"]


def run(script, env=None, libs=None):
    sources = "".join(f"source {LIB / name}\n" for name in (libs or LIB_FILES))
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        [BASH, "-c", sources + script],
        capture_output=True,
        text=True,
        env=full_env,
    )


def fake_bin(tmp_path, name, body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return bin_dir


@pytest.mark.parametrize(
    "name,expected",
    [
        ("ubuntu", "apt"),
        ("linuxmint", "apt"),
        ("fedora", "dnf"),
        ("rocky", "dnf"),
        ("arch", "pacman"),
        ("manjaro", "pacman"),
        ("opensuse-tumbleweed", "zypper"),
        ("gentoo", "unknown"),
    ],
)
def test_detect_pkg_manager(name, expected):
    result = run(
        "detect_platform() { echo linux; }\ndetect_pkg_manager",
        env={"OS_RELEASE_FILE": str(FIXTURES / name)},
    )
    assert result.stdout.strip() == expected


def test_detect_pkg_manager_missing_file_is_unknown(tmp_path):
    result = run(
        "detect_platform() { echo linux; }\ndetect_pkg_manager",
        env={"OS_RELEASE_FILE": str(tmp_path / "absent")},
    )
    assert result.stdout.strip() == "unknown"


def test_pkg_names_dnf_uses_ffmpeg_free():
    assert run("pkg_names dnf").stdout.strip() == "ffmpeg-free tmux curl"


def test_pkg_names_other_managers():
    assert run("pkg_names apt").stdout.strip() == "ffmpeg tmux curl"
    assert run("pkg_names brew").stdout.strip() == "ffmpeg tmux"


def test_install_command_pacman():
    result = run("install_command pacman ffmpeg tmux curl")
    assert result.stdout.strip() == "pacman -S --needed --noconfirm ffmpeg tmux curl"


def test_install_command_apt_dnf_zypper_brew():
    assert (
        run("install_command apt a b").stdout.strip()
        == "apt-get update && apt-get install -y a b"
    )
    assert run("install_command dnf a b").stdout.strip() == "dnf install -y a b"
    assert (
        run("install_command zypper a b").stdout.strip()
        == "zypper --non-interactive install a b"
    )
    assert run("install_command brew a b").stdout.strip() == "brew install a b"


def test_missing_commands_lists_absent(tmp_path):
    bin_dir = fake_bin(tmp_path, "tmux", "exit 0")
    result = run("missing_commands", env={"PATH": str(bin_dir)})
    assert result.stdout.strip() == "ffmpeg curl"


def test_missing_commands_empty_when_all_present(tmp_path):
    for name in ("ffmpeg", "tmux", "curl"):
        bin_dir = fake_bin(tmp_path, name, "exit 0")
    result = run("missing_commands", env={"PATH": str(bin_dir)})
    assert result.stdout.strip() == ""


def test_target_user_prefers_sudo_user():
    result = run(
        "target_user", env={"CURRENT_EUID": "0", "SUDO_USER": "alice"}
    )
    assert result.stdout.strip() == "alice"


def test_target_user_root_without_sudo_user():
    env = dict(os.environ, CURRENT_EUID="0")
    env.pop("SUDO_USER", None)
    result = subprocess.run(
        [BASH, "-c", f"source {LIB / 'user.sh'}; target_user"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == CURRENT_USER


def test_target_user_ignores_sudo_user_root():
    result = run("target_user", env={"CURRENT_EUID": "0", "SUDO_USER": "root"})
    assert result.stdout.strip() == CURRENT_USER


def test_target_user_non_root_ignores_sudo_user():
    result = run("target_user", env={"CURRENT_EUID": "1000", "SUDO_USER": "alice"})
    assert result.stdout.strip() == CURRENT_USER


def test_as_user_runs_directly_when_not_root():
    result = run(
        'TARGET_USER=alice as_user printf "%s|" "a b" c',
        env={"CURRENT_EUID": "1000"},
    )
    assert result.stdout == "a b|c|"


def test_as_user_uses_sudo_when_root(tmp_path):
    bin_dir = fake_bin(tmp_path, "sudo", 'printf "%s|" "$@"')
    result = run(
        'TARGET_USER=alice as_user echo "a b"',
        env={"CURRENT_EUID": "0", "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert result.stdout == "-u|alice|-H|echo|a b|"


def test_node_major_parses_version(tmp_path):
    bin_dir = fake_bin(tmp_path, "node", 'echo "v20.11.1"')
    result = run(f"node_major {bin_dir}/node")
    assert result.stdout.strip() == "20"


def test_node_major_empty_when_missing(tmp_path):
    result = run(f"node_major {tmp_path}/nope")
    assert result.stdout.strip() == ""


def test_node_arch_and_constants():
    result = run('echo "$(node_arch) $NODE_MIN_MAJOR $NODE_LTS_MAJOR"')
    arch, minimum, lts = result.stdout.split()
    assert arch in ("x64", "arm64")
    assert (minimum, lts) == ("20", "24")


@pytest.mark.parametrize(
    "output,expected",
    [
        ('{"loggedIn": true}', 0),
        ('{"loggedIn":true,"x":1}', 0),
        ('{"loggedIn": false}', 1),
        ('{"loggedIn":false}', 1),
    ],
)
def test_claude_logged_in(tmp_path, output, expected):
    bin_dir = fake_bin(tmp_path, "claude", f"echo '{output}'")
    result = run(f"claude_logged_in {bin_dir}/claude")
    assert result.returncode == expected


@pytest.mark.parametrize("name", LIB_FILES)
def test_libs_parse_under_bash_posix(name):
    result = subprocess.run(
        [BASH, "--posix", "-n", str(LIB / name)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_as_user_falls_back_to_sudo_user_and_runs_from_root(tmp_path):
    bin_dir = fake_bin(tmp_path, "sudo", 'printf "%s|" "$@"; pwd')
    env = {
        "CURRENT_EUID": "0",
        "SUDO_USER": "alice",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    env_clean = dict(os.environ, **env)
    env_clean.pop("TARGET_USER", None)
    result = subprocess.run(
        [BASH, "-c", f'source {LIB / "user.sh"}; cd {tmp_path}; as_user echo "a b"'],
        capture_output=True,
        text=True,
        env=env_clean,
    )
    assert result.stdout == "-u|alice|-H|echo|a b|/\n"


def make_node_dist(tmp_path, checksum=None):
    import hashlib
    import tarfile

    arch = run("node_arch").stdout.strip()
    os_name = "darwin" if os.uname().sysname == "Darwin" else "linux"
    name, mode = f"node-v24.1.0-{os_name}-{arch}.tar.gz", "w:gz"
    dist = tmp_path / "dist"
    dist.mkdir()
    payload = tmp_path / "payload" / "node-v24.1.0" / "bin"
    payload.mkdir(parents=True)
    (payload / "node").write_text("#!/bin/sh\necho v24.1.0\n")
    (payload / "node").chmod(0o755)
    tarball = dist / name
    with tarfile.open(tarball, mode) as tar:
        tar.add(tmp_path / "payload" / "node-v24.1.0", arcname="node-v24.1.0")
    digest = checksum or hashlib.sha256(tarball.read_bytes()).hexdigest()
    (dist / "SHASUMS256.txt").write_text(
        f"{'0' * 64}  node-v24.1.0-other.tar.gz\n{digest}  {name}\n"
        f"{'1' * 64}  {name[:-3]}.xz\n"
    )
    return dist


def test_install_private_node_replaces_existing_dest(tmp_path):
    dist = make_node_dist(tmp_path)
    dest = tmp_path / "home" / "node"
    dest.mkdir(parents=True)
    (dest / "old").write_text("old")
    result = run(f'NODE_DIST_URL="file://{dist}"; install_private_node {dest}')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{dest}/bin/node"
    assert (dest / "bin" / "node").exists()
    assert not (dest / "old").exists()
    assert sorted(p.name for p in dest.parent.iterdir()) == ["node"]


def test_install_private_node_mismatch_keeps_existing_dest(tmp_path):
    dist = make_node_dist(tmp_path, checksum="f" * 64)
    dest = tmp_path / "home" / "node"
    dest.mkdir(parents=True)
    (dest / "old").write_text("old")
    result = run(f'NODE_DIST_URL="file://{dist}"; install_private_node {dest}')
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
    assert (dest / "old").read_text() == "old"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["node"]


def test_ensure_uv_fails_loudly_when_download_fails(tmp_path):
    bin_dir = fake_bin(tmp_path, "curl", "exit 22")
    (tmp_path / "home").mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TARGET_USER": CURRENT_USER,
        "HOME": str(tmp_path / "home"),
    }
    result = run("user_home() { echo $HOME; }; ensure_uv", env=env)
    assert result.returncode == 1
    assert "Could not install uv" in result.stderr
