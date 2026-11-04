#!/bin/sh
# RGB Deck installer for any Linux desktop.
#   ./install.sh            user install (~/.local), asks for sudo only for the udev rule
#   ./install.sh --system   system-wide (/usr/local), needs sudo
#   ./install.sh --uninstall
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-user}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; return 1; }; }

check_deps() {
    need python3 || exit 1
    if ! python3 -c "import PySide6" 2>/dev/null; then
        cat <<EOF
PySide6 (Qt for Python) is not installed. Install it with your package manager:
  Arch/CachyOS:   sudo pacman -S pyside6
  Fedora:         sudo dnf install python3-pyside6
  Debian/Ubuntu:  sudo apt install python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore
  (or: pipx install "$HERE"  — pulls PySide6 from PyPI)
EOF
        exit 1
    fi
}

install_udev() {
    if [ -w /etc/udev/rules.d ]; then
        cp "$HERE/rgbdeck/data/70-rgbdeck.rules" /etc/udev/rules.d/
        udevadm control --reload && udevadm trigger
    else
        echo "Installing the udev rule (device permissions) — sudo needed:"
        sudo cp "$HERE/rgbdeck/data/70-rgbdeck.rules" /etc/udev/rules.d/ &&
        sudo udevadm control --reload && sudo udevadm trigger || echo "  (skipped; devices may need root until the rule is installed)"
    fi
}

case "$MODE" in
  user)
    check_deps
    BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"; ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
    mkdir -p "$BIN" "$APPS" "$ICONS"
    cat > "$BIN/rgbdeck" <<EOF
#!/bin/sh
exec python3 -c 'import sys; sys.path.insert(0, "$HERE"); from rgbdeck.app import main; sys.exit(main(sys.argv))' "\$@"
EOF
    chmod +x "$BIN/rgbdeck"
    sed "s|^Exec=.*|Exec=$BIN/rgbdeck|" "$HERE/rgbdeck/data/rgbdeck.desktop" > "$APPS/rgbdeck.desktop"
    cp "$HERE/rgbdeck/data/rgbdeck.svg" "$ICONS/rgbdeck.svg"
    update-desktop-database "$APPS" 2>/dev/null || true
    install_udev
    echo "Installed. Run 'rgbdeck' or find RGB Deck in your app menu."
    case ":$PATH:" in *":$BIN:"*) ;; *) echo "Note: $BIN is not on your PATH.";; esac
    ;;
  --system)
    check_deps
    sudo python3 -m pip install --prefix /usr/local "$HERE" 2>/dev/null || sudo pip install --prefix /usr/local --break-system-packages "$HERE"
    sudo install -Dm644 "$HERE/rgbdeck/data/rgbdeck.desktop" /usr/local/share/applications/rgbdeck.desktop
    sudo install -Dm644 "$HERE/rgbdeck/data/rgbdeck.svg" /usr/local/share/icons/hicolor/scalable/apps/rgbdeck.svg
    sudo install -Dm644 "$HERE/rgbdeck/data/70-rgbdeck.rules" /etc/udev/rules.d/70-rgbdeck.rules
    sudo udevadm control --reload && sudo udevadm trigger
    echo "Installed system-wide."
    ;;
  --uninstall)
    rm -f "$HOME/.local/bin/rgbdeck" "$HOME/.local/share/applications/rgbdeck.desktop" \
          "$HOME/.local/share/icons/hicolor/scalable/apps/rgbdeck.svg" "$HOME/.config/autostart/rgbdeck-apply.desktop" \
          "$HOME/.config/autostart/rgbdeck.desktop"
    echo "Removed user install. Your presets in ~/.config/rgbdeck were kept."
    echo "To remove the udev rule: sudo rm /etc/udev/rules.d/70-rgbdeck.rules"
    ;;
  *) echo "usage: $0 [--system|--uninstall]"; exit 2;;
esac
