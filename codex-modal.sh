#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
caller_dir=$(pwd -P)
venv_dir="$script_dir/.venv"

find_venv_python() {
    if [ -x "$venv_dir/bin/python" ]; then
        printf '%s\n' "$venv_dir/bin/python"
    elif [ -f "$venv_dir/Scripts/python.exe" ]; then
        printf '%s\n' "$venv_dir/Scripts/python.exe"
    fi
}

venv_python=$(find_venv_python || true)
if [ -z "$venv_python" ]; then
    if command -v python3 >/dev/null 2>&1; then
        bootstrap_python=python3
    elif command -v python >/dev/null 2>&1; then
        bootstrap_python=python
    else
        echo "Python 3.10 or newer is required (tried python3 and python)." >&2
        exit 1
    fi
    echo "Creating codex-modal's local Python environment..."
    "$bootstrap_python" -m venv "$venv_dir"
    venv_python=$(find_venv_python || true)
    if [ -z "$venv_python" ]; then
        echo "Could not find Python in the newly created virtual environment." >&2
        exit 1
    fi
fi

export CODEX_MODAL_CALLER_CWD="$caller_dir"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
cd "$script_dir"
exec "$venv_python" -m codex_modal "$@"
