# uv and Claude Code installation helpers. Meant to be sourced.

# Print the path of tool NAME if it is on PATH or in ~/.local/bin.
_find_tool() {
    local name="$1" found home
    found=$(command -v "$name" 2>/dev/null) || found=""
    if [ -z "$found" ]; then
        home=$(user_home)
        [ -x "$home/.local/bin/$name" ] && found="$home/.local/bin/$name"
    fi
    printf '%s\n' "$found"
}

# Download the installer at URL to a temp file, run it with SHELL_BIN as the
# target user, and remove it. Returns non-zero on any failure.
_run_installer() {
    local url="$1" shell_bin="$2" file status=0
    file=$(mktemp "${TMPDIR:-/tmp}/installer.XXXXXX") || return 1
    if curl -fsSL -o "$file" "$url" && chmod 755 "$file"; then
        as_user "$shell_bin" "$file" >&2 || status=$?
    else
        status=1
    fi
    rm -f "$file"
    return "$status"
}

# Install uv for the target user if missing, then print its path.
ensure_uv() {
    local found
    found=$(_find_tool uv)
    if [ -z "$found" ]; then
        if ! _run_installer https://astral.sh/uv/install.sh sh; then
            echo "Could not install uv" >&2
            return 1
        fi
        found=$(_find_tool uv)
    fi
    if [ -z "$found" ]; then
        echo "Could not install uv" >&2
        return 1
    fi
    echo "$found"
}

# Install Claude Code for the target user if missing, then print its path.
ensure_claude() {
    local found
    found=$(_find_tool claude)
    if [ -z "$found" ]; then
        if ! _run_installer https://claude.ai/install.sh bash; then
            echo "Could not install Claude Code" >&2
            return 1
        fi
        found=$(_find_tool claude)
    fi
    if [ -z "$found" ]; then
        echo "Could not install Claude Code" >&2
        return 1
    fi
    echo "$found"
}

# Exit 0 when `CLAUDE auth status --json` reports "loggedIn": true and a real
# request succeeds. The status only reads the local credentials, which stay on
# disk after the server has stopped accepting them.
claude_logged_in() {
    "$1" auth status --json 2>/dev/null </dev/null | grep -Eq '"loggedIn"[[:space:]]*:[[:space:]]*true' || return 1
    "$1" -p "Reply with ok." >/dev/null 2>&1 </dev/null
}
