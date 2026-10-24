# systemd user units and linger. Meant to be sourced after user.sh.

SYSTEMD_UNITS="claude-orchestrator whatsapp-bridge"

# Print TEMPLATE with @REPO@ and @NODE@ replaced. A line that is exactly
# @ENVIRONMENT@ becomes ENVIRONMENT, or is dropped when ENVIRONMENT is empty.
render_unit() {
    local template="$1" repo="$2" node="$3" environment="$4" line
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$line" = "@ENVIRONMENT@" ]; then
            [ -n "$environment" ] && printf '%s\n' "$environment"
            continue
        fi
        line=${line//@REPO@/$repo}
        line=${line//@NODE@/$node}
        printf '%s\n' "$line"
    done < "$template"
}

# Run `systemctl --user` for TARGET_USER. Root acting for another user
# reaches that user's manager through --machine.
_systemctl_user_systemd() {
    local euid="${CURRENT_EUID:-$EUID}"
    local user="${TARGET_USER:-$(target_user)}"
    if [ "$euid" = "0" ] && [ "$user" != "root" ]; then
        systemctl --user --machine="$user@" "$@"
    else
        systemctl --user "$@"
    fi
}

unit_dir_systemd() {
    printf '%s\n' "$(user_home)/.config/systemd/user"
}

# Render both units from REPO/deploy/systemd into the user's unit folder,
# then reload, enable, start and restart them.
install_units_systemd() {
    local repo="$1" node="$2" environment="$3" dir name units=""
    dir=$(unit_dir_systemd)
    as_user mkdir -p "$dir" || return 1
    for name in $SYSTEMD_UNITS; do
        render_unit "$repo/deploy/systemd/$name.service.in" "$repo" "$node" "$environment" \
            | as_user tee "$dir/$name.service" >/dev/null || return 1
        units="${units:+$units }$name.service"
    done
    _systemctl_user_systemd daemon-reload || return 1
    # shellcheck disable=SC2086
    _systemctl_user_systemd enable --now $units || return 1
    # shellcheck disable=SC2086
    _systemctl_user_systemd restart $units
}

# Stop the bridge service when it is running, so pairing owns the auth state.
stop_bridge_systemd() {
    if _systemctl_user_systemd is-active --quiet whatsapp-bridge.service; then
        _systemctl_user_systemd stop whatsapp-bridge.service
    fi
}

enable_linger_systemd() {
    loginctl enable-linger "$1"
}

disable_linger_systemd() {
    loginctl disable-linger "$1"
}

# Stop and disable both units, delete their files and reload systemd.
uninstall_units_systemd() {
    local dir
    dir=$(unit_dir_systemd)
    _systemctl_user_systemd disable --now whatsapp-bridge.service claude-orchestrator.service \
        || echo "Could not disable the services (they may not be installed)" >&2
    as_user rm -f "$dir/whatsapp-bridge.service" "$dir/claude-orchestrator.service" || return 1
    _systemctl_user_systemd daemon-reload
}
