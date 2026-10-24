# Platform and package manager detection. Meant to be sourced.

detect_platform() {
    case "$(uname -s)" in
        Darwin) echo macos ;;
        *) echo linux ;;
    esac
}

# Print the value of KEY from an os-release style file, without quotes.
_os_release_value() {
    local key="$1" file="$2" line value
    line=$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1)
    value=${line#*=}
    value=${value#\"}
    value=${value%\"}
    value=${value#\'}
    value=${value%\'}
    printf '%s\n' "$value"
}

_pkg_manager_for_id() {
    case "$1" in
        ubuntu|debian|linuxmint|pop|raspbian|elementary|kali) echo apt ;;
        fedora|rhel|centos|rocky|almalinux|ol|amzn) echo dnf ;;
        arch|manjaro|endeavouros|cachyos) echo pacman ;;
        opensuse*|suse|sles|sled) echo zypper ;;
        *) echo "" ;;
    esac
}

detect_pkg_manager() {
    local file="${OS_RELEASE_FILE:-/etc/os-release}"
    local id id_like word manager

    if [ "$(detect_platform)" = "macos" ]; then
        echo brew
        return 0
    fi
    if [ ! -r "$file" ]; then
        echo unknown
        return 0
    fi

    id=$(_os_release_value ID "$file")
    manager=$(_pkg_manager_for_id "$id")
    if [ -n "$manager" ]; then
        echo "$manager"
        return 0
    fi

    id_like=$(_os_release_value ID_LIKE "$file")
    for word in $id_like; do
        manager=$(_pkg_manager_for_id "$word")
        if [ -n "$manager" ]; then
            echo "$manager"
            return 0
        fi
    done
    echo unknown
}
