#!/usr/bin/env bash
set -euo pipefail
rm -f "$HOME/.local/bin/spotify-widget.py" \
      "$HOME/.local/share/applications/spotify-mini.desktop" \
      "$HOME/.config/autostart/spotify-mini.desktop"
echo "Desinstalado. La configuración sigue en ~/.config/spotify-widget (bórrala si quieres)."
