#!/usr/bin/env bash
# Runs inside a distro container as root: installs sudo, git and tar, copies the
# repo from /repo (read-only mount) to /src, then checks install.sh and the tests.
# Usage: container_run.sh IMAGE
set -u

IMAGE="${1:-unknown}"
STEP="setup"

fail() {
    echo "FAIL $IMAGE: $STEP"
    exit 1
}

run_step() {
    STEP="$1"
    shift
    echo "=== $STEP"
    "$@" || fail
}

# sudo and git are the base packages. On Fedora tar is also listed: the harness
# uses it to copy the repo (the installer does not need it), and dnf skips it
# when the image already has it.
install_base() {
    # shellcheck source=/dev/null
    . /etc/os-release
    case "$ID" in
        ubuntu | debian)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update && apt-get install -y sudo git
            ;;
        fedora) dnf install -y sudo git tar ;;
        arch) pacman -Sy --noconfirm sudo git ;;
        *) echo "unsupported image: $ID"; return 1 ;;
    esac
}

create_tester() {
    useradd -m -s /bin/bash tester &&
        echo 'tester ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/tester &&
        chmod 0440 /etc/sudoers.d/tester
}

copy_repo() {
    mkdir /src &&
        tar -C /repo --exclude=./orchestrator/.venv --exclude=./bridge/node_modules \
            --exclude=./bridge/dist -cf - . | tar -C /src -xf - &&
        chown -R tester:tester /src
}

# Run ./install.sh --check as tester without sudo; it must exit 3.
check_exit_3() {
    local status
    (cd /src && sudo -u tester ./install.sh --check)
    status=$?
    echo "exit status: $status"
    [ "$status" = 3 ]
}

check_with_sudo() {
    (cd /src && sudo -u tester sudo ./install.sh --check)
}

check_opus() {
    ffmpeg -hide_banner -encoders | grep -q libopus
}

# Run the test suites as tester with the uv that the installer put in its home.
run_pytest() {
    local py="$1"
    sudo -u tester -H bash -c '
        set -e
        export PATH="$HOME/.local/bin:$PATH"
        cd /src
        UV_PROJECT_ENVIRONMENT="/tmp/venv-$1" uv run --project orchestrator --python "$1" pytest -q
        UV_PROJECT_ENVIRONMENT="/tmp/venv-$1" uv run --project orchestrator --python "$1" pytest deploy/tests -q
    ' bash "$py"
}

run_step "install sudo and git" install_base
run_step "create tester" create_tester
run_step "copy repo" copy_repo
run_step "install.sh --check as a normal user exits 3" check_exit_3
run_step "install.sh --check under sudo exits 0" check_with_sudo
run_step "ffmpeg has libopus" check_opus
run_step "pytest on Python 3.11" run_pytest 3.11
run_step "pytest on Python 3.14" run_pytest 3.14

echo "OK $IMAGE"
