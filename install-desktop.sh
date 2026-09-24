#!/usr/bin/env bash
# Install Linux Desktop Shortcut and Application Entry
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/applications"
DESKTOP_DIR="$HOME/Desktop"
ICON_PATH="$SCRIPT_DIR/electron/icon.png"
EXEC_PATH="$SCRIPT_DIR/run-desktop.sh"

mkdir -p "$APP_DIR"

DESKTOP_FILE_CONTENT="[Desktop Entry]
Version=1.0
Type=Application
Name=Chess Opening Improvement
GenericName=Chess Repertoire Trainer
Comment=Personalized Chess Repertoire Trainer & Game Analysis Platform
Exec=$EXEC_PATH
Icon=$ICON_PATH
Terminal=false
Categories=Game;BoardGame;Education;
StartupWMClass=chess-opening-improvement
Keywords=chess;opening;repertoire;lichess;stockfish;
"

# 1. Install in Application Menu
echo "$DESKTOP_FILE_CONTENT" > "$APP_DIR/chess-improvement.desktop"
chmod +x "$APP_DIR/chess-improvement.desktop"
echo "✓ Installed application launcher to $APP_DIR/chess-improvement.desktop"

# 2. Install on Desktop if directory exists
if [ -d "$DESKTOP_DIR" ]; then
    echo "$DESKTOP_FILE_CONTENT" > "$DESKTOP_DIR/ChessImprovement.desktop"
    chmod +x "$DESKTOP_DIR/ChessImprovement.desktop"
    # Mark trusted in GNOME if gio is available
    if command -v gio &> /dev/null; then
        gio set "$DESKTOP_DIR/ChessImprovement.desktop" "metadata::trusted" yes 2>/dev/null || true
    fi
    echo "✓ Created desktop shortcut on $DESKTOP_DIR/ChessImprovement.desktop"
fi

if command -v update-desktop-database &> /dev/null; then
    update-desktop-database "$APP_DIR" 2>/dev/null || true
fi

echo "Desktop installation complete!"
