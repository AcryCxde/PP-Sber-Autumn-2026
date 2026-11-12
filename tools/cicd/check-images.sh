#!/usr/bin/env bash
# Build once; archive exact local image IDs for checks and future publication.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
mode=${1:?usage: check-images.sh build|smoke|checks OUTPUT [component]}
output=${2:?output directory required}
component=${3:-all}
mkdir -p "$output"
output=$(cd "$output" && pwd)
cd "$root"

# Each invocation owns only the container recorded in its private cidfile.
cid_dir=
cleanup() {
    local id
    if [[ -n $cid_dir ]]; then
        if [[ -s $cid_dir/container ]]; then
            id=$(< "$cid_dir/container")
            [[ ! $id =~ ^[0-9a-f]{64}$ ]] || docker rm -f "$id" >/dev/null 2>&1 || true
        fi
        rm -rf "$cid_dir"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

owned_run() {
    cleanup
    cid_dir=$(mktemp -d)
    docker run --rm --network none --cidfile "$cid_dir/container" "$@" &
    wait "$!"
}

archive() {
    local name=$1
    docker image inspect --format '{{.Id}}' "pps-ci:$name" > "$output/$name.image-id"
    docker image save --output "$output/$name.tar" "pps-ci:$name"
}

build() {
    local name=$1 context=$2 file=$3
    docker build --platform linux/amd64 --tag "pps-ci:$name" --file "$file" "$context"
    archive "$name"
}

smoke() {
    local name=$1 id actual
    id=$(< "$output/$name.image-id")
    [[ $id =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    actual=$(docker image inspect --format '{{.Id}}' "pps-ci:$name")
    [[ $actual == "$id" ]] || return 1
    case "$name" in
        backend)
            # Import/create app only: no ASGI lifespan, DB connection or migrations.
            owned_run --entrypoint python "$id" -c \
                'import backend.main, uvicorn; from importlib.metadata import version; assert version("claude-web-backend")' ;;
        runner)
            # Installed package and console script; never start the agent/model loop.
            owned_run --entrypoint python "$id" -c \
                'import ctrunner.main; from importlib.metadata import distribution; d = distribution("ctrunner"); assert any(e.name == "ctrunner" for e in d.entry_points)' ;;
        frontend)
            # nginx and both real HTML paths, without upstream services or host ports.
            owned_run --env BACKEND_URL=http://127.0.0.1:8000 \
                --entrypoint /bin/sh "$id" -ec \
                '/docker-entrypoint.sh nginx -t; nginx; trap "nginx -s quit" EXIT; wget -qO /tmp/root.html http://127.0.0.1/; wget -qO /tmp/product.html http://127.0.0.1/product/; grep -q "<html" /tmp/root.html; grep -q "<html" /tmp/product.html; test -d /usr/share/nginx/html/assets; test -d /usr/share/nginx/html/product/assets' ;;
    esac
}

[[ $component == all || $component == backend || $component == frontend || $component == runner || $component == frontend-checks || $component == product-checks ]] || exit 2
case "$mode" in
    checks)
        [[ $component == frontend-checks || $component == product-checks ]] || exit 2
        id=$(< "$output/$component.image-id")
        [[ $id =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
        [[ $(docker image inspect --format '{{.Id}}' "pps-ci:$component") == "$id" ]] || exit 1
        if [[ $component == product-checks ]]; then
            owned_run "$id" sh -ec 'test -d test; ls test; pnpm test && pnpm typecheck && pnpm lint'
        else
            owned_run "$id" sh -ec 'npm test && npm run lint'
        fi ;;
    build)
        for name in backend frontend runner; do
            [[ $component == all || $component == "$name" ]] || continue
            if [[ $name == runner ]]; then
                build runner runner runner/Dockerfile
            else
                build "$name" . "$name/Dockerfile"
            fi
            if [[ $name == frontend ]]; then
                # Tag cached, already-built stages; do not rebuild the final nginx image.
                for pair in build:frontend-checks product:product-checks; do
                    stage=${pair%%:*}
                    tag=${pair#*:}
                    docker build --platform linux/amd64 --target "$stage" \
                        --tag "pps-ci:$tag" --file frontend/Dockerfile .
                    archive "$tag"
                done
            fi
        done ;;
    smoke)
        for name in backend frontend runner; do
            [[ $component == all || $component == "$name" ]] || continue
            smoke "$name"
        done ;;
    *) exit 2 ;;
esac
