#!/usr/bin/env bash
# Install the WhatsApp <-> Claude Code remote control as systemd user services.
# Run ./install.sh --help for the flags and exit codes.
# SC2016: `sh -c '... "$1" ...'` scripts expand their own arguments.
# SC2088: a literal ~ is written to the config as the user typed it.
# shellcheck disable=SC2016,SC2088
set -eu

REPO=$(cd "$(dirname "$0")" && pwd)
LIB="$REPO/deploy/install"

usage() {
    cat <<'EOF'
Usage: ./install.sh [--check] [--login-only] [--uninstall [--purge]] [--help]

Installs the orchestrator and the WhatsApp bridge as systemd user services for
the current user, or for $SUDO_USER when run through sudo. Plain root (no
$SUDO_USER) installs for root with permission_mode "auto".

Steps: system packages (ffmpeg, tmux, curl; needs root), Node >= 20, uv and
Claude Code (with `claude auth login` when needed), Python and Node
dependencies, the config file (~/.config/wasap-control/config.yaml,
never overwritten), WhatsApp pairing (QR code, only when no sender is
configured), linger, and the systemd user units. The repo and Node paths may
contain spaces but not `"`, `\` or `$`.

Flags:
  --check       Report what would be done; render the config and the units
                into a temp folder and print their paths. Writes no config,
                no units, runs no systemctl or loginctl, installs no Python or
                Node dependencies and does not pair. As root it still installs
                missing system packages, Node, uv and Claude Code, because
                they are needed to check the rest; as a normal user it only
                reports them.
  --login-only  Run the services only while the user is logged in (disables
                linger). By default linger is enabled, so the services run
                from boot.
  --uninstall   Stop and disable both services and delete their unit files.
                Keeps the config and the data folders.
  --purge       Implies --uninstall. After one [y/N] confirmation, also
                deletes ~/.config/wasap-control,
                ~/.local/share/wasap-control-bridge and
                ~/.local/share/wasap-control.
  --help        Show this help.

Exit codes:
  0   done
  2   bad flag
  3   root is needed: run the printed `sudo ./install.sh ...` command
  4   unsupported system: install the printed packages yourself
  78  a check or a step failed (missing binary, login, configuration)
EOF
}

CHECK=0
LOGIN_ONLY=0
UNINSTALL=0
PURGE=0
SUDO_CMD="sudo ./install.sh"
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --login-only) LOGIN_ONLY=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --purge) PURGE=1; UNINSTALL=1 ;;
        --help|-h) usage; exit 0 ;;
        *)
            echo "Unknown argument: $arg" >&2
            usage >&2
            exit 2
            ;;
    esac
    SUDO_CMD="$SUDO_CMD $(printf '%q' "$arg")"
done
if [ "$UNINSTALL" = 1 ] && { [ "$CHECK" = 1 ] || [ "$LOGIN_ONLY" = 1 ]; }; then
    echo "--uninstall and --purge cannot be combined with --check or --login-only" >&2
    exit 2
fi

for lib in os packages node user tools systemd; do
    # shellcheck source=/dev/null
    . "$LIB/$lib.sh"
done

die() {
    local status="$1"
    shift
    printf '%s\n' "$*" >&2
    exit "$status"
}

# Print why root is needed and the command to run, then exit 3.
need_root() {
    printf '%s\n' "$1" >&2
    printf 'Run:\n  %s\n' "$SUDO_CMD" >&2
    exit 3
}

say() {
    printf '%s\n' "$*"
}

is_root() {
    [ "${CURRENT_EUID:-$EUID}" = "0" ]
}

# Print PROMPT, read one line from the terminal (or stdin when there is no
# terminal) and print it. EOF gives an empty answer.
ask() {
    local answer=""
    printf '%s' "$1" >&2
    if (exec </dev/tty) 2>/dev/null; then
        IFS= read -r answer </dev/tty || answer=""
    else
        IFS= read -r answer || answer=""
    fi
    printf '%s\n' "$answer"
}

# Run a command with the terminal as stdin when there is one.
run_interactive() {
    if (exec </dev/tty) 2>/dev/null; then
        "$@" </dev/tty
    else
        "$@"
    fi
}

if [ "$(detect_platform)" = "macos" ]; then
    die 4 "macOS support is not available yet"
fi

# --- 1. Target user ----------------------------------------------------------

TARGET_USER=$(target_user)
if [ "$TARGET_USER" = "root" ]; then
    PERMISSION_MODE=auto
else
    PERMISSION_MODE=bypassPermissions
fi
HOME_DIR=$(user_home)
[ -n "$HOME_DIR" ] || die 78 "Could not find the home folder of $TARGET_USER"
CONFIG="$HOME_DIR/.config/wasap-control/config.yaml"
PRIVATE_NODE_DIR="$HOME_DIR/.local/share/wasap-control/node"
VENV_PYTHON="$REPO/control/.venv/bin/python"
say "Installing for user $TARGET_USER ($HOME_DIR)"

# --- Uninstall ---------------------------------------------------------------

if [ "$UNINSTALL" = 1 ]; then
    uninstall_units_systemd || die 78 "Could not remove the unit files"
    say "Removed the wasap-control and wasap-control-bridge services."
    if [ "$PURGE" = 1 ]; then
        data_dirs="$HOME_DIR/.config/wasap-control $HOME_DIR/.local/share/wasap-control-bridge $HOME_DIR/.local/share/wasap-control"
        answer=$(ask "Delete $data_dirs (config, WhatsApp pairing, state)? [y/N] ")
        case "$answer" in
            y|Y|yes|YES|Yes)
                as_user rm -rf "$HOME_DIR/.config/wasap-control" \
                    "$HOME_DIR/.local/share/wasap-control-bridge" \
                    "$HOME_DIR/.local/share/wasap-control" \
                    || die 78 "Could not delete the data folders"
                say "Deleted the config and data folders."
                ;;
            *) say "Kept the config and data folders." ;;
        esac
    else
        say "Kept $CONFIG and the data folders."
    fi
    exit 0
fi

# --- 2. System packages ------------------------------------------------------

missing=$(missing_commands)
if [ -n "$missing" ]; then
    manager=$(detect_pkg_manager)
    packages=$(pkg_names "$manager")
    # shellcheck disable=SC2086
    if [ "$manager" = "unknown" ] || ! command_line=$(install_command "$manager" $packages); then
        die 4 "Missing commands: $missing. This system's package manager is not supported. Install these packages yourself, then run ./install.sh again: $packages"
    fi
    if ! is_root; then
        need_root "Missing commands: $missing. Installing them needs root."
    fi
    say "Installing system packages: $command_line"
    sh -c "$command_line" || die 78 "Could not install the system packages: $packages"
    missing=$(missing_commands)
    [ -z "$missing" ] || die 78 "Still missing after the package install: $missing"
else
    say "System packages: ok"
fi

# --- 3. Node, uv, Claude Code ------------------------------------------------

# Print the path of a node binary with major version >= NODE_MIN_MAJOR and an
# npm next to it: the system one, else the private one.
find_node() {
    local candidate major
    for candidate in "$(command -v node 2>/dev/null || true)" "$PRIVATE_NODE_DIR/bin/node"; do
        [ -n "$candidate" ] && [ -x "$candidate" ] || continue
        [ -x "$(dirname "$candidate")/npm" ] || continue
        major=$(node_major "$candidate")
        if [ -n "$major" ] && [ "$major" -ge "$NODE_MIN_MAJOR" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# Only root installs tools under --check.
may_install() {
    [ "$CHECK" = 0 ] || is_root
}

if NODE=$(find_node); then
    say "Node: $NODE"
elif may_install; then
    say "Installing Node $NODE_LTS_MAJOR into $PRIVATE_NODE_DIR"
    NODE=$(as_user bash -c '. "$1" && install_private_node "$2"' bash "$LIB/node.sh" "$PRIVATE_NODE_DIR") \
        || die 78 "Could not install Node"
else
    NODE="$PRIVATE_NODE_DIR/bin/node"
    say "Node: would install Node $NODE_LTS_MAJOR into $PRIVATE_NODE_DIR"
fi

UV=""
CLAUDE=""
if may_install; then
    UV=$(ensure_uv) || die 78 "uv is required"
    CLAUDE=$(ensure_claude) || die 78 "Claude Code is required"
else
    UV=$(_find_tool uv)
    CLAUDE=$(_find_tool claude)
fi
if [ -n "$UV" ]; then say "uv: $UV"; else say "uv: would install uv"; fi
if [ -n "$CLAUDE" ]; then say "Claude Code: $CLAUDE"; else say "Claude Code: would install Claude Code"; fi

_claude_as_user() {
    as_user "$CLAUDE" "$@"
}

if [ -n "$CLAUDE" ]; then
    if claude_logged_in _claude_as_user; then
        say "Claude Code login: ok"
    elif [ "$CHECK" = 1 ]; then
        say "Claude Code login: would run claude auth login"
    else
        say "Claude Code is not logged in. Running: claude auth login"
        run_interactive as_user "$CLAUDE" auth login || true
        claude_logged_in _claude_as_user || die 78 "Claude Code is not logged in. Run 'claude auth login', then ./install.sh again."
    fi
else
    say "Claude Code login: would run claude auth login"
fi

# --- 4. Python and Node dependencies ------------------------------------------

GPU=0
if nvidia-smi >/dev/null 2>&1; then
    GPU=1
fi
WHISPER_DEVICE=cpu
SYNC_EXTRA=""
if [ "$GPU" = 1 ]; then
    WHISPER_DEVICE=cuda
    SYNC_EXTRA=" --extra cuda"
fi

# Run uv sync for the orchestrator, with the cuda extra on a GPU machine.
uv_sync() {
    if [ "$GPU" = 1 ]; then
        as_user "$UV" sync --project "$REPO/control" --extra cuda
    else
        as_user "$UV" sync --project "$REPO/control"
    fi
}

if [ "$CHECK" = 1 ]; then
    say "Would run: uv sync --project $REPO/control$SYNC_EXTRA"
    say "Would run: npm ci && npm run build (in $REPO/bridge, with $NODE)"
else
    say "Running: uv sync --project $REPO/control$SYNC_EXTRA"
    uv_sync || die 78 "uv sync failed"
    say "Running: npm ci && npm run build (in $REPO/bridge)"
    as_user env PATH="$(dirname "$NODE"):$PATH" sh -c 'cd "$1" && npm ci && npm run build' sh "$REPO/bridge" \
        || die 78 "Building the bridge failed"
fi

# Run the config writer with the orchestrator's Python.
install_config() {
    as_user "$VENV_PYTHON" -m wasap_control.install_config "$@"
}

# Print the config differences from deploy/config.example.yaml, if any.
report_config_diff() {
    local report
    report=$("$@" diff --path "$CONFIG") || die 78 "Could not read $CONFIG"
    if [ -n "$report" ]; then
        say "Differences from deploy/config.example.yaml:"
        say "$report"
    fi
}

# Print the user's answer to the project_root question.
ask_project_root() {
    local answer
    while :; do
        answer=$(ask "Folder that holds your projects (project_root) [~/projects]: ")
        [ -n "$answer" ] || answer="~/projects"
        case "$answer" in
            "~"|"~/"*|/*) printf '%s\n' "$answer"; return 0 ;;
            *) echo "Use an absolute path or one starting with ~/" >&2 ;;
        esac
    done
}

# Print PATH with a leading ~ replaced by the target user's home.
expand_home() {
    case "$1" in
        "~") printf '%s\n' "$HOME_DIR" ;;
        "~/"*) printf '%s\n' "$HOME_DIR/${1#\~/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# Print the LD_LIBRARY_PATH line for the venv's nvidia libraries, if any.
cuda_environment() {
    local dirs
    [ "$GPU" = 1 ] && [ -x "$VENV_PYTHON" ] || return 0
    dirs=$("$VENV_PYTHON" -c '
import glob, os
try:
    import nvidia
except ImportError:
    raise SystemExit(0)
dirs = sorted(d for p in nvidia.__path__ for d in glob.glob(os.path.join(p, "*", "lib")))
print(":".join(dirs))
' 2>/dev/null) || dirs=""
    [ -z "$dirs" ] || printf 'Environment="LD_LIBRARY_PATH=%s"\n' "$dirs"
}

# --- --check: render into a temp folder and stop ----------------------------

if [ "$CHECK" = 1 ]; then
    check_dir=$(mktemp -d "${TMPDIR:-/tmp}/install-check.XXXXXX")
    if [ -e "$CONFIG" ]; then
        say "Config: $CONFIG exists and is kept"
        if [ -x "$VENV_PYTHON" ]; then
            report_config_diff "$VENV_PYTHON" -m wasap_control.install_config
        fi
    elif [ -x "$VENV_PYTHON" ]; then
        "$VENV_PYTHON" -m wasap_control.install_config write --path "$check_dir/config.yaml" \
            --project-root "~/projects" --claude-binary "${CLAUDE:-$HOME_DIR/.local/bin/claude}" \
            --whisper-device "$WHISPER_DEVICE" --permission-mode "$PERMISSION_MODE" \
            || die 78 "Could not render the config"
        say "Config: $check_dir/config.yaml"
    else
        say "Config: not rendered, $REPO/control/.venv does not exist yet (uv sync creates it)"
    fi
    environment=$(cuda_environment)
    for name in $SYSTEMD_UNITS; do
        render_unit "$REPO/deploy/systemd/$name.service.in" "$REPO" "$NODE" "$environment" \
            > "$check_dir/$name.service" || die 78 "Could not render $name.service"
        say "Unit: $check_dir/$name.service"
    done
    say "Check complete. Rendered files are in $check_dir"
    exit 0
fi

# --- 5. Config ---------------------------------------------------------------

if [ -e "$CONFIG" ]; then
    say "Config: $CONFIG exists and is kept"
    report_config_diff install_config
else
    project_root=$(ask_project_root)
    as_user mkdir -p "$(expand_home "$project_root")" || die 78 "Could not create $project_root"
    install_config write --path "$CONFIG" --project-root "$project_root" \
        --claude-binary "$CLAUDE" --whisper-device "$WHISPER_DEVICE" \
        --permission-mode "$PERMISSION_MODE" || die 78 "Could not write $CONFIG"
    say "Config: wrote $CONFIG"
fi

# --- 6. WhatsApp pairing -----------------------------------------------------

sender=$(install_config get-sender --path "$CONFIG") || die 78 "Could not read $CONFIG"
if [ -z "$sender" ]; then
    say "Pairing WhatsApp: scan the QR code with WhatsApp > Linked devices."
    stop_bridge_systemd || die 78 "Could not stop wasap-control-bridge.service"
    pair_output=$(as_user sh -c 'cd "$1" && exec "$2" dist/index.js --pair' sh "$REPO/bridge" "$NODE") \
        || die 78 "WhatsApp pairing failed"
    sender=$(printf '%s\n' "$pair_output" | sed -n 's/^PAIRED //p' | head -n 1)
    [ -n "$sender" ] || die 78 "WhatsApp pairing did not report a number"
    install_config set-sender --path "$CONFIG" --jid "$sender" || die 78 "Could not write $CONFIG"
    say "Paired with $sender"
else
    say "WhatsApp: paired with $sender"
fi

# --- 7. Linger and the user manager ------------------------------------------

if [ "$LOGIN_ONLY" = 0 ]; then
    if ! enable_linger_systemd "$TARGET_USER"; then
        is_root && die 78 "loginctl enable-linger $TARGET_USER failed"
        need_root "Turning on linger for $TARGET_USER needs root."
    fi
    say "Linger: on (the services run from boot)"
fi
wait_user_manager_systemd "$TARGET_USER" \
    || die 78 "The systemd user manager of $TARGET_USER is not running (no /run/user/<uid>/bus). Log in as $TARGET_USER, or run without --login-only, then run ./install.sh again."

# --- 8. systemd user units ----------------------------------------------------

install_units_systemd "$REPO" "$NODE" "$(cuda_environment)" \
    || die 78 "Could not install or start the services"
say "Services: wasap-control and wasap-control-bridge started"

if [ "$LOGIN_ONLY" = 1 ]; then
    if ! disable_linger_systemd "$TARGET_USER"; then
        is_root && die 78 "loginctl disable-linger $TARGET_USER failed"
        need_root "Turning off linger for $TARGET_USER needs root."
    fi
    say "Linger: off (the services run only while $TARGET_USER is logged in)"
fi

say "Done."
