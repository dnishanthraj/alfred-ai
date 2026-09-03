#!/bin/bash
# Double-click launcher for the Alfred web console.
#
# Replaces the old version, which drove Ghostty through System Events
# keystrokes — that needed Accessibility permission, broke if the terminal
# wasn't named "Ghostty", and left the app running inside a scratch tab.
# This just starts the server and opens the browser.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [ ! -x "venv/bin/python" ]; then
  echo "No virtualenv found at $DIR/venv"
  echo "Create one first:  python3.11 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  read -r -p "Press return to close."
  exit 1
fi

if ! pgrep -f "ollama serve" > /dev/null 2>&1; then
  echo "Ollama isn't running — starting it."
  ollama serve > /dev/null 2>&1 &
  sleep 2
fi

echo "Starting the B.A.T. console…"
exec venv/bin/python run.py
