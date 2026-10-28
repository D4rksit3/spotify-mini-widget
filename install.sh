#!/usr/bin/env bash
# Instala el widget para el usuario actual (sin sudo).
set -euo pipefail
cd "$(dirname "$0")"

BIN="$HOME/.local/bin/spotify-widget.py"
APPS="$HOME/.local/share/applications"

install -Dm755 spotify-widget.py "$BIN"
mkdir -p "$APPS"
sed "s|@BIN@|$BIN|" spotify-mini.desktop > "$APPS/spotify-mini.desktop"

if [[ "${1:-}" == "--autostart" ]]; then
    mkdir -p "$HOME/.config/autostart"
    cp "$APPS/spotify-mini.desktop" "$HOME/.config/autostart/"
    echo "Se abrirá solo al iniciar sesión."
fi

echo "Listo. Búscalo como \"Spotify mini\" en el menú de aplicaciones."
