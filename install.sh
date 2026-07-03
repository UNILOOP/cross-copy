#!/usr/bin/env bash
#
# cross-copy installer
#
# Usage:
#   ./install.sh                 # from a cloned checkout
#   curl -fsSL https://raw.githubusercontent.com/sayeed99/cross-copy/main/install.sh | bash
#
# Options:
#   --service    also install a login service (systemd user unit / launchd agent)
#                so the cross-copy daemon starts automatically at login.
#
# Environment:
#   REPO_URL     override the git repo used when not running from a checkout.
#
# Compatible with macOS's stock bash 3.2 and any modern Linux bash.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sayeed99/cross-copy.git}"

INSTALL_SERVICE=0
for arg in "$@"; do
    case "$arg" in
        --service) INSTALL_SERVICE=1 ;;
        -h|--help)
            sed -n '2,16p' "$0" 2>/dev/null || true
            exit 0
            ;;
        *)
            echo "Unknown option: $arg (supported: --service)" >&2
            exit 2
            ;;
    esac
done

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Detect OS
# ---------------------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Darwin|Linux) ;;
    *) die "Unsupported OS: $OS (cross-copy supports macOS and Linux)" ;;
esac

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

SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
    SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
    if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
        SRC="$SCRIPT_DIR"
        info "Installing from local checkout: $SRC"
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

cleanup() {
    if [ -n "$TMP_CLONE" ] && [ -d "$TMP_CLONE" ]; then
        rm -rf "$TMP_CLONE"
    fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 4. Install: prefer pipx, fall back to a dedicated venv
# ---------------------------------------------------------------------------
VENV_DIR="$HOME/.local/share/cross-copy/venv"
BIN_DIR="$HOME/.local/bin"
CCP=""

if command -v pipx >/dev/null 2>&1; then
    info "Installing with pipx ..."
    pipx install --force "$SRC"
    if command -v ccp >/dev/null 2>&1; then
        CCP="$(command -v ccp)"
    else
        # pipx installs into ~/.local/bin by default
        CCP="$BIN_DIR/ccp"
    fi
else
    info "pipx not found — installing into a dedicated venv at $VENV_DIR"
    rm -rf "$VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PYTHON" -m venv "$VENV_DIR" \
        || die "Failed to create a venv. On Debian/Ubuntu you may need:  sudo apt install python3-venv"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet "$SRC"

    mkdir -p "$BIN_DIR"
    ln -sf "$VENV_DIR/bin/ccp" "$BIN_DIR/ccp"
    CCP="$BIN_DIR/ccp"
    info "Linked $BIN_DIR/ccp -> $VENV_DIR/bin/ccp"
fi

# ---------------------------------------------------------------------------
# 5. PATH check for ~/.local/bin
# ---------------------------------------------------------------------------
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        warn "$BIN_DIR is not on your PATH."
        echo "  Add it by appending this line to your shell config:" >&2
        echo "" >&2
        echo "    export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
        echo "" >&2
        echo "  (~/.bashrc for bash, ~/.zshrc for zsh — then restart your shell.)" >&2
        ;;
esac

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------
[ -x "$CCP" ] || die "Install finished but $CCP is missing or not executable."
info "Verifying install ..."
"$CCP" version || die "'ccp version' failed — the install did not complete correctly."

# ---------------------------------------------------------------------------
# 7. Optional: install autostart service (--service)
# ---------------------------------------------------------------------------
if [ "$INSTALL_SERVICE" -eq 1 ]; then
    if [ "$OS" = "Linux" ]; then
        # ---- systemd user unit (Linux) ----
        UNIT_DIR="$HOME/.config/systemd/user"
        UNIT_FILE="$UNIT_DIR/cross-copy.service"
        info "Installing systemd user service: $UNIT_FILE"
        mkdir -p "$UNIT_DIR"
        cat > "$UNIT_FILE" <<EOF
[Unit]
Description=cross-copy network clipboard daemon

[Service]
ExecStart=$CCP daemon run
Restart=on-failure

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now cross-copy.service
        info "Service enabled. Check it with: systemctl --user status cross-copy"
    else
        # ---- launchd agent (macOS) ----
        PLIST_DIR="$HOME/Library/LaunchAgents"
        PLIST_FILE="$PLIST_DIR/com.crosscopy.daemon.plist"
        info "Installing launchd agent: $PLIST_FILE"
        mkdir -p "$PLIST_DIR"
        cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.crosscopy.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$CCP</string>
        <string>daemon</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
        launchctl load "$PLIST_FILE"
        info "Launch agent loaded. The daemon will start at login."
    fi
fi

# ---------------------------------------------------------------------------
# 8. Quickstart
# ---------------------------------------------------------------------------
echo ""
info "cross-copy installed!"
cat <<'EOF'

Quickstart:

  ccp copy notes.pdf     # on machine A: put a file on the network clipboard
  ccp paste              # on machine B: the file appears in the current dir
  ccp devices            # see other machines on your LAN
  ccp ui                 # open the web UI (drag & drop files)

The background daemon starts automatically the first time you run a command —
no setup needed. (Re-run this installer with --service to also start it at login.)
EOF
