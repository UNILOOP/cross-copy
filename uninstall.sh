#!/usr/bin/env bash
#
# cross-copy uninstaller
#
# Removes the daemon autostart service (via `ccp daemon uninstall`), the
# cross-copy package (pipx install or dedicated venv), and the `ccp` symlink.
# Optionally removes ~/.crosscopy data (asks first; default is to keep it).

set -euo pipefail

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }

OS="$(uname -s)"
VENV_DIR="$HOME/.local/share/cross-copy/venv"
BIN_LINK="$HOME/.local/bin/ccp"
DATA_DIR="${CROSSCOPY_HOME:-$HOME/.crosscopy}"
REMOVED_SOMETHING=0
PATH_MARKER_BEGIN="# >>> cross-copy PATH >>>"
PATH_MARKER_END="# <<< cross-copy PATH <<<"

remove_managed_path_block() {
    local profile="$1"
    [ -f "$profile" ] || return 0
    grep -F "$PATH_MARKER_BEGIN" "$profile" >/dev/null 2>&1 || return 0
    if ! grep -F "$PATH_MARKER_END" "$profile" >/dev/null 2>&1; then
        warn "Leaving incomplete Cross Copy PATH block in $profile unchanged."
        return 0
    fi
    local tmp
    tmp="$(mktemp "${profile}.cross-copy.XXXXXX")"
    awk -v begin="$PATH_MARKER_BEGIN" -v end="$PATH_MARKER_END" '
        $0 == begin { removing = 1; next }
        $0 == end { removing = 0; next }
        !removing { print }
    ' "$profile" > "$tmp"
    cat "$tmp" > "$profile"
    rm -f "$tmp"
    info "Removed Cross Copy PATH setup from $profile"
    REMOVED_SOMETHING=1
}

# ---------------------------------------------------------------------------
# 1. Tear down autostart + stop the daemon, while `ccp` still exists.
#    `ccp daemon uninstall` owns the service teardown (systemd unit / launchd
#    plist). Fall back to removing leftover service files by hand only if the
#    `ccp` binary is already gone.
# ---------------------------------------------------------------------------
# Prefer this HOME's install ($BIN_LINK) over whatever `ccp` happens to be
# on PATH — with multiple installs (or a customised $HOME) `command -v ccp`
# can resolve to a *different* install, and its `daemon uninstall` would
# tear down that install's service instead of this one's.
CCP_BIN=""
if [ -x "$BIN_LINK" ]; then
    CCP_BIN="$BIN_LINK"
elif command -v ccp >/dev/null 2>&1; then
    # Only trust a PATH-found ccp that lives under this $HOME; a ccp from
    # somewhere else belongs to a different install and must not have its
    # service/daemon torn down by this uninstaller.
    CCP_ON_PATH="$(command -v ccp)"
    case "$CCP_ON_PATH" in
        "$HOME"/*) CCP_BIN="$CCP_ON_PATH" ;;
        *) warn "Found ccp at $CCP_ON_PATH (outside $HOME) — leaving that install alone." ;;
    esac
fi

if [ -n "$CCP_BIN" ]; then
    info "Removing daemon autostart (ccp daemon uninstall) ..."
    "$CCP_BIN" daemon uninstall >/dev/null 2>&1 || true
    "$CCP_BIN" daemon stop >/dev/null 2>&1 || true
else
    # Best-effort cleanup of service files left behind without `ccp`.
    if [ "$OS" = "Linux" ]; then
        UNIT_FILE="$HOME/.config/systemd/user/cross-copy.service"
        if [ -f "$UNIT_FILE" ]; then
            info "Removing leftover systemd user service ..."
            systemctl --user disable --now cross-copy.service 2>/dev/null || true
            rm -f "$UNIT_FILE"
            systemctl --user daemon-reload 2>/dev/null || true
            REMOVED_SOMETHING=1
        fi
    elif [ "$OS" = "Darwin" ]; then
        PLIST_FILE="$HOME/Library/LaunchAgents/com.crosscopy.daemon.plist"
        if [ -f "$PLIST_FILE" ]; then
            info "Removing leftover launchd agent ..."
            launchctl bootout "gui/$(id -u)/com.crosscopy.daemon" 2>/dev/null \
                || launchctl unload "$PLIST_FILE" 2>/dev/null || true
            rm -f "$PLIST_FILE"
            REMOVED_SOMETHING=1
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 2. Remove the package: pipx install, or venv + symlink
# ---------------------------------------------------------------------------
# Note: capture `pipx list` output instead of piping into grep -q: with
# `set -o pipefail`, grep -q exiting early can SIGPIPE pipx (exit 141) and
# make the check false even when the package IS installed.
if command -v pipx >/dev/null 2>&1; then
    PIPX_LIST="$(pipx list 2>/dev/null || true)"
    case "$PIPX_LIST" in
        *cross-copy*)
            info "Uninstalling pipx package cross-copy ..."
            pipx uninstall cross-copy
            REMOVED_SOMETHING=1
            ;;
    esac
fi

if [ -d "$VENV_DIR" ]; then
    info "Removing venv: $VENV_DIR"
    rm -rf "$VENV_DIR"
    rmdir "$(dirname "$VENV_DIR")" 2>/dev/null || true
    REMOVED_SOMETHING=1
fi

if [ -L "$BIN_LINK" ] || [ -f "$BIN_LINK" ]; then
    info "Removing $BIN_LINK"
    rm -f "$BIN_LINK"
    REMOVED_SOMETHING=1
fi

# Remove only PATH entries written by install.sh. Existing user-managed PATH
# configuration is deliberately left untouched.
for profile in \
    "$HOME/.profile" \
    "$HOME/.bashrc" \
    "$HOME/.bash_profile" \
    "$HOME/.bash_login" \
    "$HOME/.zshrc" \
    "$HOME/.zprofile" \
    "$HOME/.cshrc" \
    "$HOME/.config/fish/conf.d/cross-copy.fish"
do
    remove_managed_path_block "$profile"
done

if [ "$REMOVED_SOMETHING" -eq 0 ]; then
    warn "No cross-copy installation found (pipx package, $VENV_DIR, or $BIN_LINK)."
fi

# ---------------------------------------------------------------------------
# 3. Optionally remove data directory (default: keep)
# ---------------------------------------------------------------------------
if [ -d "$DATA_DIR" ]; then
    REPLY="n"
    if [ -t 0 ]; then
        printf 'Remove data directory %s (device config, clipboard, logs)? [y/N] ' "$DATA_DIR"
        read -r REPLY || REPLY="n"
    else
        info "Non-interactive shell: keeping $DATA_DIR (delete it manually if you want)."
    fi
    case "$REPLY" in
        y|Y|yes|YES)
            rm -rf "$DATA_DIR"
            info "Removed $DATA_DIR"
            ;;
        *)
            info "Keeping $DATA_DIR"
            ;;
    esac
fi

info "cross-copy uninstalled."
