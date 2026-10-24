#!/usr/bin/env bash
# Runs install.sh --check and the test suites in distro containers, and the
# bridge build in Node containers. Needs Docker.
# Usage: deploy/tests/containers.sh [IMAGE...]   (default: all images)
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
LOGS=$(mktemp -d "${TMPDIR:-/tmp}/containers.XXXXXX")

DISTRO_IMAGES="ubuntu:24.04 fedora:latest archlinux:latest"
NODE_IMAGES="node:20 node:latest"

# Runs as the image's non-root `node` user on a copy of bridge/ only.
# SC2016: the script expands its own arguments inside the container.
# shellcheck disable=SC2016
NODE_SCRIPT='
set -eu
mkdir /tmp/bridge
tar -C /repo/bridge --exclude=./node_modules --exclude=./dist -cf - . | tar -C /tmp/bridge -xf -
cd /tmp/bridge
echo "=== npm ci"; npm ci || { echo "FAIL $1: npm ci"; exit 1; }
echo "=== npm test"; npm test || { echo "FAIL $1: npm test"; exit 1; }
echo "=== npm run build"; npm run build || { echo "FAIL $1: npm run build"; exit 1; }
echo "OK $1"
'

run_image() {
    local image="$1" log="$2"
    case " $NODE_IMAGES " in
        *" $image "*)
            docker run --rm --user node -v "$REPO:/repo:ro" "$image" bash -c "$NODE_SCRIPT" bash "$image" > "$log" 2>&1
            ;;
        *)
            docker run --rm -v "$REPO:/repo:ro" -v "$HERE/container_run.sh:/container_run.sh:ro" \
                "$image" bash /container_run.sh "$image" > "$log" 2>&1
            ;;
    esac
}

if [ "$#" -gt 0 ]; then
    IMAGES="$*"
else
    IMAGES="$DISTRO_IMAGES $NODE_IMAGES"
fi

echo "Logs: $LOGS"
failed=0
for image in $IMAGES; do
    log="$LOGS/$(printf '%s' "$image" | tr ':/' '__').log"
    echo "--- $image (log: $log)"
    start=$(date +%s)
    run_image "$image" "$log"
    status=$?
    elapsed=$(( $(date +%s) - start ))
    if [ "$status" = 0 ] && [ "$(tail -n 1 "$log")" = "OK $image" ]; then
        echo "OK $image (${elapsed}s)"
    else
        line=$(grep '^FAIL ' "$log" | tail -n 1)
        echo "${line:-FAIL $image: docker run exited $status}"
        echo "    last log lines:"
        tail -n 15 "$log" | sed 's/^/    | /'
        failed=1
    fi
done
exit "$failed"
