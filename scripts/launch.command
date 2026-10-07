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

# Asked over HTTP rather than found by process name: the Ollama menu-bar app
# runs its server under a different command line, so `pgrep "ollama serve"`
# missed it and started a second one. Finder also hands a double-clicked script
# a PATH without Homebrew's directories in it.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! curl -sf -o /dev/null http://127.0.0.1:11434/api/version; then
  echo "Ollama isn't running — starting it."
  if [ -d "/Applications/Ollama.app" ]; then
    open -ga Ollama
  else
    ollama serve > /dev/null 2>&1 &
  fi
  for _ in $(seq 1 40); do
    curl -sf -o /dev/null http://127.0.0.1:11434/api/version && break
    sleep 0.5
  done
fi

echo "Starting the console…"
exec venv/bin/python run.py
