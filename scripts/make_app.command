#!/bin/bash
# Build "WayneTech Console.app" — a real macOS application you can keep in the
# Dock, rather than a browser tab you have to go and find.
#
# The bundle is generated rather than committed: an .app is a directory of
# binaries and plists, which has no business in a source tree, and building it
# here means it always points at wherever the project actually lives.
#
# What it does when launched:
#   1. starts Ollama if it isn't running
#   2. starts the console server if it isn't already up
#   3. opens the console in a chromeless browser window — no tabs, no address
#      bar, its own Dock entry
#   4. stays alive so the Dock icon is *ours*, and shuts the server down on quit
#
# Re-run this script after moving the project.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$DIR/WayneTech Console.app"
PORT="${ALFRED_WEB_PORT:-8420}"

echo "Building $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- Info.plist -------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>WayneTech Console</string>
  <key>CFBundleDisplayName</key><string>WayneTech Console</string>
  <key>CFBundleIdentifier</key><string>tech.wayne.console</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>console</string>
  <key>CFBundleIconFile</key><string>console</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# --- icon -------------------------------------------------------------------
# The aperture mark, drawn as SVG and converted through macOS's own tools so
# there is no image library to install.
ICONSET="$(mktemp -d)/console.iconset"
mkdir -p "$ICONSET"
SVG="$(mktemp).svg"
cat > "$SVG" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  <rect width="1024" height="1024" rx="228" fill="#05070b"/>
  <g fill="none" stroke="#4fa8e0" stroke-linecap="round">
    <circle cx="512" cy="512" r="372" stroke-width="18" stroke-dasharray="860 330"/>
    <circle cx="512" cy="512" r="272" stroke-width="18" stroke-dasharray="440 215"/>
    <circle cx="512" cy="512" r="172" stroke-width="18" stroke-dasharray="215 128"/>
  </g>
  <circle cx="512" cy="512" r="42" fill="#8fd3ff"/>
</svg>
SVG

# qlmanage renders SVG via QuickLook; sips then produces every size the
# iconset needs.
BASE="$(mktemp -d)/icon.png"
qlmanage -t -s 1024 -o "$(dirname "$BASE")" "$SVG" >/dev/null 2>&1 || true
RENDERED="$(dirname "$BASE")/$(basename "$SVG").png"
if [ ! -f "$RENDERED" ]; then
  echo "  (could not render the icon; the app will use the system default)"
else
  for size in 16 32 64 128 256 512; do
    sips -z $size $size "$RENDERED" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1
    sips -z $((size*2)) $((size*2)) "$RENDERED" \
         --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null 2>&1
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/console.icns" 2>/dev/null \
    && echo "  icon built"
fi

# --- launcher ---------------------------------------------------------------
cat > "$APP/Contents/MacOS/console" <<LAUNCHER
#!/bin/bash
DIR="$DIR"
PORT="$PORT"
URL="http://127.0.0.1:\$PORT"
cd "\$DIR"

# Ollama first — the console is not much use without it.
pgrep -f "ollama serve" >/dev/null 2>&1 || { ollama serve >/dev/null 2>&1 & sleep 2; }

# Only start the server if nothing is already answering on the port, so
# launching twice doesn't fight itself.
STARTED=""
if ! curl -sf -o /dev/null "\$URL" 2>/dev/null; then
  "\$DIR/venv/bin/python" run.py --no-open >/tmp/wayne-console.log 2>&1 &
  SERVER=\$!
  STARTED=1
  for _ in \$(seq 1 60); do
    curl -sf -o /dev/null "\$URL" 2>/dev/null && break
    sleep 0.5
  done
fi

# A chromeless window in its own profile: no tabs, no address bar, and it does
# not disturb whatever you already have open in your browser.
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "\$CHROME" ]; then
  "\$CHROME" --app="\$URL" \\
    --user-data-dir="\$HOME/Library/Application Support/WayneTechConsole" \\
    --window-size=1400,900 --no-first-run --no-default-browser-check >/dev/null 2>&1
else
  open "\$URL"
  # No window to wait on, so hold here until the user quits the app.
  while true; do sleep 3600; done
fi

# The window has closed. Take the server with us, but only if we started it.
[ -n "\$STARTED" ] && kill \$SERVER 2>/dev/null
exit 0
LAUNCHER

chmod +x "$APP/Contents/MacOS/console"
touch "$APP"   # nudge Finder into noticing the new bundle

echo
echo "Built: $APP"
echo "Open it once, then right-click its Dock icon → Options → Keep in Dock."
