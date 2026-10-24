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

# Install uv for the target user if missing, then print its path.
ensure_uv() {
    local found
    found=$(_find_tool uv)
    if [ -z "$found" ]; then
        as_user sh -c 'curl -fsSL https://astral.sh/uv/install.sh | sh' >&2 || return 1
        found=$(_find_tool uv)
    fi
    [ -n "$found" ] || return 1
    echo "$found"
}

# Install Claude Code for the target user if missing, then print its path.
ensure_claude() {
    local found
    found=$(_find_tool claude)
    if [ -z "$found" ]; then
        as_user bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >&2 || return 1
        found=$(_find_tool claude)
    fi
    [ -n "$found" ] || return 1
    echo "$found"
}

# Exit 0 when `CLAUDE auth status --json` reports "loggedIn": true.
claude_logged_in() {
    "$1" auth status --json 2>/dev/null | grep -Eq '"loggedIn"[[:space:]]*:[[:space:]]*true'
}
