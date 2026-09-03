#!/usr/bin/env python3
"""
Entry point.

    python run.py            # web console at http://127.0.0.1:8420
    python run.py --cli      # terminal console
    python run.py --no-open  # web console, don't open a browser
"""
import argparse
import sys
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="Alfred AI")
    parser.add_argument("--cli", action="store_true",
                        help="run the terminal console instead of the web console")
    parser.add_argument("--no-open", action="store_true",
                        help="don't open a browser window")
    args = parser.parse_args()

    if args.cli:
        from alfred.cli import main as cli_main
        cli_main()
        return

    from alfred import config
    from alfred.server import serve

    url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
    print(f"WayneTech B.A.T. Console → {url}")
    print("Press Ctrl+C to shut down.\n")

    if not args.no_open:
        # Give uvicorn a moment to bind before the browser asks for the page.
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()

    try:
        serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
