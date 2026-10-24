# Node.js detection and private install. Meant to be sourced.

NODE_MIN_MAJOR=20
NODE_LTS_MAJOR=24
NODE_DIST_URL="https://nodejs.org/dist/latest-v${NODE_LTS_MAJOR}.x"

# Print the major version of the node binary at PATH, or nothing.
node_major() {
    local version
    version=$("$1" --version 2>/dev/null) || return 0
    version=${version#v}
    case "$version" in
        [0-9]*) printf '%s\n' "${version%%.*}" ;;
    esac
}

node_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo x64 ;;
        aarch64|arm64) echo arm64 ;;
        *) uname -m ;;
    esac
}

_sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# Download the official Node LTS build into DEST after checking its SHA-256,
# then print the path of the node binary. DEST is left untouched on failure.
install_private_node() {
    local dest="$1" os ext arch sums tmp name expected actual parent

    if [ "$(uname -s)" = "Darwin" ]; then
        os=darwin
        ext=tar.gz
    else
        os=linux
        ext=tar.xz
    fi
    arch=$(node_arch)
    parent=$(dirname "$dest")
    mkdir -p "$parent" || return 1
    tmp=$(mktemp -d "$parent/.node-install.XXXXXX") || return 1

    if ! curl -fsSL -o "$tmp/SHASUMS256.txt" "$NODE_DIST_URL/SHASUMS256.txt"; then
        echo "Could not download $NODE_DIST_URL/SHASUMS256.txt" >&2
        rm -rf "$tmp"
        return 1
    fi
    sums="$tmp/SHASUMS256.txt"
    name=$(awk -v suffix="-${os}-${arch}.${ext}" '
        { n = length($2); s = length(suffix) }
        $2 ~ /^node-v[0-9.]+-/ && n > s && substr($2, n - s + 1) == suffix { print $2; exit }
    ' "$sums")
    if [ -z "$name" ]; then
        echo "No Node build for ${os}-${arch} in $NODE_DIST_URL/SHASUMS256.txt" >&2
        rm -rf "$tmp"
        return 1
    fi
    expected=$(awk -v n="$name" '$2 == n { print $1; exit }' "$sums")

    if ! curl -fsSL -o "$tmp/$name" "$NODE_DIST_URL/$name"; then
        echo "Could not download $NODE_DIST_URL/$name" >&2
        rm -rf "$tmp"
        return 1
    fi
    actual=$(_sha256_of "$tmp/$name")
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        echo "SHA-256 mismatch for $name: expected ${expected:-none}, got $actual" >&2
        rm -rf "$tmp"
        return 1
    fi

    mkdir "$tmp/extract" || { rm -rf "$tmp"; return 1; }
    if ! tar -xf "$tmp/$name" -C "$tmp/extract" --strip-components=1; then
        echo "Could not extract $name" >&2
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$dest"
    if ! mv "$tmp/extract" "$dest"; then
        echo "Could not move Node into $dest" >&2
        rm -rf "$tmp"
        return 1
    fi
    rm -rf "$tmp"
    echo "$dest/bin/node"
}
