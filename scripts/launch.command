#!/bin/bash
# Double-click launcher for the WayneTech console.
#
# Starts Ollama if it isn't already running, then serves the console and opens
# it in the browser. The previous version drove Ghostty through System Events
# keystrokes, which needed Accessibility permission and broke if the terminal
# wasn't named "Ghostty".

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [ ! -x "venv/bin/python" ]; then
  echo "No virtualenv found at $DIR/venv"
  echo "Create one first:"
  echo "  python3.11 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  read -r -p "Press return to close."
  exit 1
fi

if ! pgrep -f "ollama serve" > /dev/null 2>&1; then
  echo "Ollama isn't running — starting it."
  ollama serve > /dev/null 2>&1 &
  sleep 2
fi

echo "Starting the console…"
exec venv/bin/python run.py
