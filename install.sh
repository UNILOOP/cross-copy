#!/usr/bin/env bash
#
# cross-copy installer
#
# Usage:
#   ./install.sh                 # from a cloned checkout
#   curl -fsSL https://raw.githubusercontent.com/UNILOOP/cross-copy/main/install.sh | bash
#
# Options:
#   --no-service   skip setting up daemon autostart. By default the installer
#                  runs `ccp daemon install` so the daemon starts at login
#                  (systemd user unit on Linux, launchd agent on macOS).
#                  Enable it later any time with:  ccp daemon install
#   --service      accepted for back-compat; autostart is now the default.
#   --path-only    configure ~/.local/bin for the default shell, then exit.
#
# Environment:
#   REPO_URL          override the git repo used when not running from a checkout.
#   CROSSCOPY_SHELL   override shell detection for PATH setup (e.g. zsh, bash).
#   CROSSCOPY_NO_SHELL_RELOAD=1  do not start a refreshed interactive shell.
#
# Compatible with macOS's stock bash 3.2 and any modern Linux bash.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/UNILOOP/cross-copy.git}"

INSTALL_SERVICE=1
PATH_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --no-service) INSTALL_SERVICE=0 ;;
        --service) ;; # back-compat no-op: autostart is now the default
        --path-only) PATH_ONLY=1 ;;
        -h|--help)
            # In `curl | bash` mode $0 is not this script; fall back to a
            # short usage message instead of printing nothing.
            if [ -f "$0" ] && sed -n '2,19p' "$0" 2>/dev/null; then
                :
            else
                echo "Usage: install.sh [--no-service] [--path-only]"
                echo "  --no-service   skip daemon autostart setup (ccp daemon install)"
                echo "  --path-only    only add ~/.local/bin to the default shell PATH"
            fi
            exit 0
            ;;
        *)
            echo "Unknown option: $arg (supported: --no-service, --service, --path-only)" >&2
            exit 2
            ;;
    esac
done

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

BIN_DIR="$HOME/.local/bin"
PATH_MARKER_BEGIN="# >>> cross-copy PATH >>>"
PATH_MARKER_END="# <<< cross-copy PATH <<<"

profile_has_managed_path() {
    [ -f "$1" ] && grep -F "$PATH_MARKER_BEGIN" "$1" >/dev/null 2>&1
}

append_posix_path_block() {
    local profile="$1"
    if profile_has_managed_path "$profile"; then
        info "$profile already has the Cross Copy PATH setup."
        return
    fi
    mkdir -p "$(dirname "$profile")"
    if [ -s "$profile" ]; then
        printf '\n' >> "$profile"
    fi
    cat >> "$profile" <<'EOF'
# >>> cross-copy PATH >>>
case ":$PATH:" in
    *:"$HOME/.local/bin":*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
# <<< cross-copy PATH <<<
EOF
    info "Added ~/.local/bin to PATH in $profile."
}

append_fish_path_block() {
    local profile="$HOME/.config/fish/conf.d/cross-copy.fish"
    if profile_has_managed_path "$profile"; then
        info "$profile already has the Cross Copy PATH setup."
        return
    fi
    mkdir -p "$(dirname "$profile")"
    if [ -s "$profile" ]; then
        printf '\n' >> "$profile"
    fi
    cat >> "$profile" <<'EOF'
# >>> cross-copy PATH >>>
if not contains -- "$HOME/.local/bin" $PATH
    set -gx PATH "$HOME/.local/bin" $PATH
end
# <<< cross-copy PATH <<<
EOF
    info "Added ~/.local/bin to PATH in $profile."
}

append_csh_path_block() {
    local profile="$HOME/.cshrc"
    if profile_has_managed_path "$profile"; then
        info "$profile already has the Cross Copy PATH setup."
        return
    fi
    if [ -s "$profile" ]; then
        printf '\n' >> "$profile"
    fi
    cat >> "$profile" <<'EOF'
# >>> cross-copy PATH >>>
set path = ( "$HOME/.local/bin" $path )
# <<< cross-copy PATH <<<
EOF
    info "Added ~/.local/bin to PATH in $profile."
}

configure_shell_path() {
    local shell_path="${CROSSCOPY_SHELL:-${SHELL:-}}"
    local shell_name="${shell_path##*/}"
    if [ -z "$shell_name" ]; then
        shell_name="bash"
    fi

    case "$shell_name" in
        zsh)
            append_posix_path_block "$HOME/.zshrc"
            ;;
        bash)
            append_posix_path_block "$HOME/.bashrc"
            if [ -f "$HOME/.bash_profile" ]; then
                append_posix_path_block "$HOME/.bash_profile"
            elif [ -f "$HOME/.bash_login" ]; then
                append_posix_path_block "$HOME/.bash_login"
            else
                append_posix_path_block "$HOME/.profile"
            fi
            ;;
        fish)
            append_fish_path_block
            ;;
        csh|tcsh)
            append_csh_path_block
            ;;
        *)
            append_posix_path_block "$HOME/.profile"
            ;;
    esac

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) export PATH="$BIN_DIR:$PATH" ;;
    esac
}

start_refreshed_shell() {
    [ "${CROSSCOPY_NO_SHELL_RELOAD:-0}" = "1" ] && return
    [ -t 1 ] || return
    [ -r /dev/tty ] || return

    local shell_path="${CROSSCOPY_SHELL:-${SHELL:-}}"
    if [ -z "$shell_path" ]; then
        shell_path="$(command -v bash 2>/dev/null || true)"
    elif [ "${shell_path#/}" = "$shell_path" ]; then
        shell_path="$(command -v "$shell_path" 2>/dev/null || true)"
    fi
    if [ -z "$shell_path" ] || [ ! -x "$shell_path" ]; then
        warn "Could not locate your login shell. Open a new terminal to use ccp."
        return
    fi

    info "Reloading $(basename "$shell_path") so ccp is available in this terminal."
    # exec replaces this installer process, so its EXIT trap would not run.
    # Clean the temporary source checkout first when a full install created it.
    if type cleanup >/dev/null 2>&1; then
        cleanup
        trap - EXIT
    fi
    exec "$shell_path" -l < /dev/tty
}

# ---------------------------------------------------------------------------
# 1. Detect OS
# ---------------------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Darwin|Linux) ;;
    *) die "Unsupported OS: $OS (cross-copy supports macOS and Linux)" ;;
esac

if [ "$PATH_ONLY" -eq 1 ]; then
    configure_shell_path
    info "PATH setup complete."
    start_refreshed_shell
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. Find a suitable Python (>= 3.9)
# ---------------------------------------------------------------------------
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PYTHON="$(command -v "$candidate")"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    if [ "$OS" = "Darwin" ]; then
        die "Python 3.9+ not found. Install it with:  brew install python3"
    else
        die "Python 3.9+ not found. Install it with your package manager, e.g.:
  sudo apt install python3 python3-venv    (Debian/Ubuntu)
  sudo dnf install python3                 (Fedora/RHEL)"
    fi
fi
info "Using Python: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"

# ---------------------------------------------------------------------------
# 3. Resolve the package source (<src>)
#    - If this script lives in a checkout with pyproject.toml, install that.
#    - Otherwise (curl | bash), clone REPO_URL into a temp dir.
# ---------------------------------------------------------------------------
SRC=""
TMP_CLONE=""

# Register the cleanup trap BEFORE any mktemp/clone below: if the clone (or
# anything after mktemp) dies, the temp dir must still be removed.
cleanup() {
    if [ -n "$TMP_CLONE" ] && [ -d "$TMP_CLONE" ]; then
        rm -rf "$TMP_CLONE"
    fi
}
trap cleanup EXIT

SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
    if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
        info "Installing from local checkout: $SCRIPT_DIR"
        # Install from a clean temp copy of the checkout, not the checkout
        # itself: setuptools reuses a stale in-tree build/ dir (mtime-based),
        # which can silently install OLD code, and in-tree builds litter the
        # checkout with build/ and *.egg-info.
        TMP_CLONE="$(mktemp -d "${TMPDIR:-/tmp}/cross-copy.XXXXXX")"
        cp -R "$SCRIPT_DIR" "$TMP_CLONE/src"
        rm -rf "$TMP_CLONE/src/build" "$TMP_CLONE/src/dist" \
               "$TMP_CLONE/src/.git" "$TMP_CLONE/src/.venv" \
               "$TMP_CLONE/src/venv" "$TMP_CLONE/src"/*.egg-info
        SRC="$TMP_CLONE/src"
    fi
fi

if [ -z "$SRC" ]; then
    command -v git >/dev/null 2>&1 || die "git is required to fetch cross-copy. Install git and retry."
    TMP_CLONE="$(mktemp -d "${TMPDIR:-/tmp}/cross-copy.XXXXXX")"
    info "Cloning $REPO_URL ..."
    git clone --depth 1 "$REPO_URL" "$TMP_CLONE/cross-copy" >/dev/null 2>&1 \
        || die "Failed to clone $REPO_URL"
    SRC="$TMP_CLONE/cross-copy"
fi

# ---------------------------------------------------------------------------
# 4. Install: prefer pipx, fall back to a dedicated venv
# ---------------------------------------------------------------------------
VENV_DIR="$HOME/.local/share/cross-copy/venv"
CCP=""

if command -v pipx >/dev/null 2>&1; then
    info "Installing with pipx ..."
    pipx install --force "$SRC"
    # Tray-widget extras (pystray/Pillow; native panel bindings on macOS).
    WIDGET_PKGS="pystray Pillow"
    if [ "$OS" = "Darwin" ]; then
        WIDGET_PKGS="$WIDGET_PKGS pyobjc-framework-WebKit"
    fi
    # shellcheck disable=SC2086
    if ! pipx inject cross-copy $WIDGET_PKGS >/dev/null 2>&1; then
        warn "Could not install the tray-widget extras."
        warn "The tray icon needs them:  pipx inject cross-copy $WIDGET_PKGS"
    fi
    # Prefer the binary pipx just installed (~/.local/bin by default) over
    # `command -v ccp`, which can resolve to a different, pre-existing
    # install elsewhere on PATH.
    if [ -x "$BIN_DIR/ccp" ]; then
        CCP="$BIN_DIR/ccp"
    elif command -v ccp >/dev/null 2>&1; then
        CCP="$(command -v ccp)"
    else
        CCP="$BIN_DIR/ccp"
    fi
else
    info "pipx not found — installing into a dedicated venv at $VENV_DIR"
    rm -rf "$VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    # --system-site-packages on Linux: the tray icon renders through the
    # desktop's AppIndicator support (PyGObject), which is a system package
    # that pip cannot build cleanly — the venv must be able to see it.
    VENV_FLAGS=""
    if [ "$OS" = "Linux" ]; then
        VENV_FLAGS="--system-site-packages"
    fi
    # shellcheck disable=SC2086
    "$PYTHON" -m venv $VENV_FLAGS "$VENV_DIR" \
        || die "Failed to create a venv. On Debian/Ubuntu you may need:  sudo apt install python3-venv"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    # Include the tray-widget extras (pystray/Pillow); fall back to a bare
    # install if the extras can't be built on this machine.
    if ! "$VENV_DIR/bin/pip" install --quiet "$SRC[widget]" 2>/dev/null; then
        warn "Tray-widget extras failed to install; installing the base package."
        warn "The tray icon needs them:  $VENV_DIR/bin/pip install pystray Pillow"
        "$VENV_DIR/bin/pip" install --quiet "$SRC"
    fi

    mkdir -p "$BIN_DIR"
    ln -sf "$VENV_DIR/bin/ccp" "$BIN_DIR/ccp"
    CCP="$BIN_DIR/ccp"
    info "Linked $BIN_DIR/ccp -> $VENV_DIR/bin/ccp"
fi

# ---------------------------------------------------------------------------
# 5. Add ~/.local/bin to the user's shell PATH
# ---------------------------------------------------------------------------
configure_shell_path

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------
[ -x "$CCP" ] || die "Install finished but $CCP is missing or not executable."
info "Verifying install ..."
"$CCP" version || die "'ccp version' failed — the install did not complete correctly."

# ---------------------------------------------------------------------------
# 6b. Native Finder/file-manager quick actions
# ---------------------------------------------------------------------------
info "Installing file-manager context actions ..."
if "$CCP" context install; then
    info "Right-click sharing actions installed."
else
    warn "Could not install file-manager actions (not fatal)."
    warn "Retry later with:  ccp context install"
fi

# ---------------------------------------------------------------------------
# 7. Daemon autostart (default; skip with --no-service)
#    `ccp daemon install` owns the systemd-unit/launchd-plist details.
# ---------------------------------------------------------------------------
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    info "Setting up daemon autostart (ccp daemon install) ..."
    if "$CCP" daemon install; then
        info "Daemon autostart enabled — it will start at login and is running now."
    else
        if [ "$OS" = "Darwin" ]; then
            warn "Could not set up autostart (launchctl could not load the launchd agent — see the error above for details)."
        else
            warn "Could not set up autostart (e.g. no systemd on this system)."
        fi
        warn "This is not fatal: the daemon still auto-starts on first 'ccp' use."
        warn "You can retry later with:  ccp daemon install"
    fi
else
    info "Skipping autostart setup (--no-service)."
    info "Enable it later any time with:  ccp daemon install"
fi

# ---------------------------------------------------------------------------
# 7b. Tray widget autostart (only when a graphical session is present)
# ---------------------------------------------------------------------------
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    if [ "$OS" = "Darwin" ] || [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
        info "Setting up the tray widget (ccp widget install) ..."
        if "$CCP" widget install; then
            info "Tray widget enabled — look for the cross-copy icon in your status bar."
        else
            warn "Could not set up the tray widget (not fatal)."
            warn "Run it manually with:  ccp widget    or retry:  ccp widget install"
        fi
    else
        info "No graphical session detected — skipping the tray widget."
        info "Enable it later from a desktop session with:  ccp widget install"
    fi
fi

# ---------------------------------------------------------------------------
# 8. Quickstart
# ---------------------------------------------------------------------------
echo ""
info "cross-copy installed!"
cat <<'EOF'

Quickstart:

  ccp copy notes.pdf        # on machine A: put a file on the network clipboard
  ccp copy "meeting at 5"   # ...or put a snippet of text on it
  ccp paste                 # on machine B: the file appears / the text prints
  ccp devices               # see other machines on your LAN
  ccp ui                    # open the web UI (drag & drop files, send text)

The daemon now starts at login automatically (unless you used --no-service —
enable later with 'ccp daemon install'), and it also auto-starts the first
time you run a ccp command. No further setup needed.
EOF

start_refreshed_shell
