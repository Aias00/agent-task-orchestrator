#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

python_supported() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -IB -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1
}

find_python() {
    if [ -n "${ATO_INSTALL_PYTHON:-}" ]; then
        python_supported "$ATO_INSTALL_PYTHON" || fail 'ATO_INSTALL_PYTHON must select Python 3.12 or later.'
        printf '%s\n' "$ATO_INSTALL_PYTHON"
        return
    fi

    for candidate in python3 python3.12 python3.13 python3.14; do
        if python_supported "$candidate"; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    fail 'ATO requires Python 3.12 or later. Install Python and rerun the installer.'
}

main() {
    case "${1:-}" in
        -h|--help)
            cat <<'EOF'
Install ATO from a local checkout or with curl | sh.

Options: --bin-dir PATH, --releases-dir PATH, --skills-dir PATH
Pi/ACP:  --with-pi --wheelhouse PATH --npm-cache PATH

ATO_INSTALL_PYTHON selects a Python 3.12+ executable.
ATO_INSTALL_REF fetches a specific Git branch, tag, or commit instead of main.
EOF
            return
            ;;
    esac

    command -v git >/dev/null 2>&1 || fail 'ATO requires Git. Install Git and rerun the installer.'
    ato_python=$(find_python)
    if [ -z "${ATO_INSTALL_REF:-}" ] && [ -f "$0" ]; then
        ato_source=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
        if [ -f "$ato_source/scripts/quick_install.py" ]; then
            exec "$ato_python" -IB "$ato_source/scripts/quick_install.py" "$@"
        fi
    fi

    ato_checkout=$(mktemp -d "${TMPDIR:-/tmp}/ato-install.XXXXXXXX")
    trap 'rm -rf -- "$ato_checkout"' 0
    trap 'exit 1' HUP INT TERM
    git init --quiet "$ato_checkout"
    git -C "$ato_checkout" fetch --quiet --depth=1 -- \
        https://github.com/Aias00/agent-task-orchestrator.git "${ATO_INSTALL_REF:-main}"
    git -C "$ato_checkout" checkout --quiet --detach FETCH_HEAD

    [ -f "$ato_checkout/scripts/quick_install.py" ] || fail 'The selected ATO revision has no quick installer.'
    "$ato_python" -IB "$ato_checkout/scripts/quick_install.py" "$@"
}

main "$@"
