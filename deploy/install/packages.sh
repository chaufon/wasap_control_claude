# System package names, missing-command check and install commands. Meant to be sourced.

REQUIRED_COMMANDS="ffmpeg tmux curl"

pkg_names() {
    case "$1" in
        dnf) echo "ffmpeg-free tmux curl" ;;
        brew) echo "ffmpeg tmux" ;;
        *) echo "ffmpeg tmux curl" ;;
    esac
}

# Print, space-separated on one line, the required commands not found on PATH.
missing_commands() {
    local cmd missing=""
    for cmd in $REQUIRED_COMMANDS; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing="${missing:+$missing }$cmd"
        fi
    done
    printf '%s\n' "$missing"
}

# Print the command that installs PKGS with MANAGER. The caller runs it.
install_command() {
    local manager="$1"
    shift
    case "$manager" in
        apt) echo "apt-get update && apt-get install -y $*" ;;
        dnf) echo "dnf install -y $*" ;;
        pacman) echo "pacman -S --needed --noconfirm $*" ;;
        zypper) echo "zypper --non-interactive install $*" ;;
        brew) echo "brew install $*" ;;
        *) return 1 ;;
    esac
}
