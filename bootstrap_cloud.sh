#!/usr/bin/env bash
# Project-local, foreground-only runtime setup. Never launches a browser/service.
set -euo pipefail
umask 077

readonly PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly RUNTIME_DIR="$PROJECT_DIR/.runtime"
readonly UV_VERSION=0.13.6
readonly UV_ARCHIVE=uv-x86_64-unknown-linux-gnu.tar.gz
readonly UV_URL="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ARCHIVE"
readonly UV_SHA256=f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645
readonly PYTHON_VERSION=3.12.16
readonly PYTHON_BUILD=20261026
readonly UV_BIN="$RUNTIME_DIR/bin/uv"
readonly VENV_DIR="$PROJECT_DIR/.venv"

fail() { printf '%s\n' "$*" >&2; exit 1; }
usage() {
    printf '%s\n' 'Usage: bash bootstrap_cloud.sh [uv|python|venv|deps|check|all]'
    printf '%s\n' 'Each stage runs in the foreground. Default: all. No sudo or service startup.'
}

stage=${1:-all}
if [[ $# -gt 1 ]]; then usage >&2; exit 2; fi
case "$stage" in
    -h|--help) usage; exit 0 ;;
    uv|python|venv|deps|check|all) ;;
    *) usage >&2; exit 2 ;;
esac

[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || fail 'Requires Linux x86_64.'
for tool in curl sha256sum tar timeout env mkdir mv; do
    command -v "$tool" >/dev/null || fail "Missing required command: $tool"
done
[[ -f "$PROJECT_DIR/requirements-core.txt" ]] || fail 'requirements-core.txt is missing.'
[[ ! -L "$RUNTIME_DIR" && ! -L "$VENV_DIR" ]] || fail 'Runtime/venv paths must not be symlinks.'

# Do not use the account home directory or inherit uv/index/credential settings.
# env -i is limited to uv's public, unauthenticated runtime/package installation.
run_uv() {
    [[ -x "$UV_BIN" ]] || fail 'Run the uv stage first.'
    timeout --signal=TERM --kill-after=5s 120s env -i \
        PATH=/usr/bin:/bin \
        TMPDIR="$RUNTIME_DIR/tmp" \
        UV_NO_CONFIG=1 \
        UV_NO_PROGRESS=1 \
        UV_CACHE_DIR="$RUNTIME_DIR/cache" \
        UV_CREDENTIALS_DIR="$RUNTIME_DIR/credentials" \
        UV_PYTHON_INSTALL_DIR="$RUNTIME_DIR/python" \
        UV_PYTHON_BIN_DIR="$RUNTIME_DIR/bin" \
        UV_PYTHON_CACHE_DIR="$RUNTIME_DIR/python-cache" \
        UV_PYTHON_CPYTHON_BUILD="$PYTHON_BUILD" \
        UV_TOOL_DIR="$RUNTIME_DIR/tools" \
        UV_TOOL_BIN_DIR="$RUNTIME_DIR/bin" \
        UV_HTTP_CONNECT_TIMEOUT=10 \
        UV_HTTP_TIMEOUT=60 \
        UV_HTTP_RETRIES=0 \
        UV_CONCURRENT_DOWNLOADS=2 \
        UV_CONCURRENT_INSTALLS=2 \
        UV_CONCURRENT_BUILDS=1 \
        "$UV_BIN" "$@"
}

prepare_dirs() {
    mkdir -p "$RUNTIME_DIR/bin" "$RUNTIME_DIR/downloads" "$RUNTIME_DIR/tmp"
}

install_uv() {
    local archive_path="$RUNTIME_DIR/downloads/$UV_ARCHIVE"
    local partial_path="$archive_path.part"
    printf '%s\n' "Downloading uv $UV_VERSION from its official GitHub release."
    curl --disable --fail --location --silent --show-error \
        --proto '=https' --proto-redir '=https' \
        --connect-timeout 10 --max-time 90 --retry 0 \
        "$UV_URL" --output "$partial_path"
    printf '%s  %s\n' "$UV_SHA256" "$partial_path" | sha256sum --check --status - \
        || fail 'uv archive SHA256 mismatch; nothing was extracted.'
    mv -- "$partial_path" "$archive_path"
    tar --extract --gzip --file "$archive_path" --strip-components=1 \
        --directory "$RUNTIME_DIR/bin" --no-same-owner
    run_uv --version
}

install_python() {
    printf '%s\n' "Installing managed Python $PYTHON_VERSION into the project runtime."
    run_uv python install "$PYTHON_VERSION"
}

create_venv() {
    printf '%s\n' 'Creating/checking the project virtual environment.'
    if [[ -e "$VENV_DIR" ]]; then
        [[ -x "$VENV_DIR/bin/python" ]] || fail 'Existing .venv is incomplete; inspect it before continuing.'
        timeout --signal=TERM --kill-after=5s 20s "$VENV_DIR/bin/python" -B -c \
            'import sys; assert sys.version_info[:3] == (3, 12, 16), "Existing .venv uses a different Python version"'
        return
    fi
    run_uv venv --managed-python --python "$PYTHON_VERSION" "$VENV_DIR"
}

install_deps() {
    [[ -x "$VENV_DIR/bin/python" ]] || fail 'Run the venv stage first.'
    printf '%s\n' 'Installing core dependencies from PyPI wheels only.'
    run_uv pip install --python "$VENV_DIR/bin/python" \
        --default-index https://pypi.org/simple --only-binary :all: \
        -r "$PROJECT_DIR/requirements-core.txt"
}

check_runtime() {
    [[ -x "$VENV_DIR/bin/python" ]] || fail 'Run the venv and deps stages first.'
    [[ -f "$PROJECT_DIR/cloud_probe.py" ]] || fail 'cloud_probe.py is missing.'
    timeout --signal=TERM --kill-after=5s 20s "$VENV_DIR/bin/python" -B -c \
        'import sys; assert sys.version_info[:3] == (3, 12, 16), "Unexpected Python version"'
    timeout --signal=TERM --kill-after=5s 20s \
        "$VENV_DIR/bin/python" -B "$PROJECT_DIR/cloud_probe.py" --self-check
    printf '%s\n' 'Runtime self-check completed. No Douyin query, browser or service was started.'
}

prepare_dirs
case "$stage" in
    uv) install_uv ;;
    python) install_python ;;
    venv) create_venv ;;
    deps) install_deps ;;
    check) check_runtime ;;
    all) install_uv; install_python; create_venv; install_deps; check_runtime ;;
esac
