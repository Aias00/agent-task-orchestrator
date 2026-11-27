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
    fail 'ATO requires Python 3.12 or later.'
}

usage() {
    cat <<'EOF'
Install ATO v2 (lightweight editable or venv installation).

Usage: sh ./install.sh [options]
Options:
  --bin-dir PATH       Launcher directory (default: ~/.local/bin)
  --venv-dir PATH      Virtualenv directory (default: ~/.local/share/ato/venv)
  --skills-dir PATH    Optional Codex skills directory (e.g. ~/.codex/skills)
  --editable           Install local checkout in editable mode (changes take effect immediately)
  --server             Opt in to pip installation of HTTP/MCP server dependencies

Environment:
  ATO_INSTALL_PYTHON   Select Python 3.12+ binary
  ATO_INSTALL_REF      Git branch, tag, or commit when installing via curl | sh
EOF
}

main() {
    BIN_DIR="$HOME/.local/bin"
    VENV_DIR="$HOME/.local/share/ato/venv"
    SKILLS_DIR=""
    EDITABLE=0
    SERVER=0

    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help) usage; return 0 ;;
            --bin-dir) [ "$#" -ge 2 ] && [ -n "$2" ] || fail '--bin-dir requires a non-empty path'; BIN_DIR="$2"; shift 2 ;;
            --venv-dir) [ "$#" -ge 2 ] && [ -n "$2" ] || fail '--venv-dir requires a non-empty path'; VENV_DIR="$2"; shift 2 ;;
            --skills-dir) [ "$#" -ge 2 ] && [ -n "$2" ] || fail '--skills-dir requires a non-empty path'; SKILLS_DIR="$2"; shift 2 ;;
            --editable) EDITABLE=1; shift ;;
            --server) SERVER=1; shift ;;
            *) fail "Unknown option: $1" ;;
        esac
    done

    [ "$BIN_DIR" != "$SKILLS_DIR" ] || fail '--bin-dir and --skills-dir must be distinct'
    command -v git >/dev/null 2>&1 || fail 'ATO requires Git.'
    PY=$(find_python)

    SRC=""
    TMP_CHECKOUT=""
    if [ -z "${ATO_INSTALL_REF:-}" ]; then
        case "$(basename -- "$0")" in
            install.sh)
                if [ -f "$0" ]; then
                    CANDIDATE=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
                    if [ -f "$CANDIDATE/pyproject.toml" ] && [ -d "$CANDIDATE/ato" ]; then
                        SRC="$CANDIDATE"
                    fi
                fi
                ;;
        esac
    fi

    if [ -z "$SRC" ]; then
        [ "$EDITABLE" -eq 0 ] || fail '--editable requires running install.sh from a local checkout'
        TMP_CHECKOUT=$(mktemp -d "${TMPDIR:-/tmp}/ato-install.XXXXXXXX")
        trap 'rm -rf -- "$TMP_CHECKOUT"' 0
        trap 'exit 1' HUP INT TERM
        git init --quiet "$TMP_CHECKOUT"
        git -C "$TMP_CHECKOUT" fetch --quiet --depth=1 -- \
            https://github.com/Aias00/agent-task-orchestrator.git "${ATO_INSTALL_REF:-main}"
        git -C "$TMP_CHECKOUT" checkout --quiet --detach FETCH_HEAD
        SRC="$TMP_CHECKOUT"
    fi

    mkdir -p "$BIN_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PY" -m venv "$VENV_DIR"

    SITE=$("$VENV_DIR/bin/python" -c 'import site; print(site.getsitepackages()[0])')
    if [ "$EDITABLE" -eq 1 ]; then
        printf '%s\n' "$SRC" > "$SITE/ato_editable.pth"
        ASSETS_ROOT="$SRC"
    else
        rm -rf "$SITE/ato" "$SITE/ato_editable.pth"
        cp -R "$SRC/ato" "$SITE/ato"
        ASSETS_ROOT="$VENV_DIR/share/ato"
        mkdir -p "$ASSETS_ROOT"
        cp "$SRC/pi-path-guard.mjs" "$ASSETS_ROOT/"
    fi

    if [ "$SERVER" -eq 1 ]; then
        if [ "$EDITABLE" -eq 1 ]; then
            "$VENV_DIR/bin/python" -m pip install -e "$SRC[server]"
        else
            "$VENV_DIR/bin/python" -m pip install "$SRC[server]"
        fi
    fi

    cat > "$BIN_DIR/ato.tmp" <<EOF
#!/bin/sh
exec "$VENV_DIR/bin/python" -m ato.cli "\$@"
EOF
    chmod 0755 "$BIN_DIR/ato.tmp"
    mv "$BIN_DIR/ato.tmp" "$BIN_DIR/ato"

    if [ -n "$SKILLS_DIR" ]; then
        mkdir -p "$SKILLS_DIR/ato"
        cp "$SRC/skills/ato/SKILL.md" "$SKILLS_DIR/ato/SKILL.md"
        chmod 0644 "$SKILLS_DIR/ato/SKILL.md"
    fi

    printf 'Installed ato v2 -> %s (venv: %s)\n' "$BIN_DIR/ato" "$VENV_DIR"
}

main "$@"
