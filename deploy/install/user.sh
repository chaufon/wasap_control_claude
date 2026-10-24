# Target user resolution. Meant to be sourced.

# Print the user the installation is for: the invoking user when run through
# sudo, otherwise the current user.
target_user() {
    local euid="${CURRENT_EUID:-$EUID}"
    if [ "$euid" = "0" ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        echo "$SUDO_USER"
    else
        id -un
    fi
}

# Run a command as TARGET_USER (default: target_user) from "/" when root and
# the target is not root.
as_user() {
    local euid="${CURRENT_EUID:-$EUID}"
    local user="${TARGET_USER:-$(target_user)}"
    if [ "$euid" = "0" ] && [ "$user" != "root" ]; then
        (cd / && sudo -u "$user" -H "$@")
    else
        (cd / && "$@")
    fi
}

# Print the home directory of TARGET_USER (or the current user when unset).
user_home() {
    local user="${TARGET_USER:-$(target_user)}" home
    if [ "$(uname -s)" = "Darwin" ]; then
        home=$(dscl . -read "/Users/$user" NFSHomeDirectory 2>/dev/null | awk '{print $2}')
    else
        home=$(getent passwd "$user" | cut -d: -f6)
    fi
    printf '%s\n' "$home"
}
