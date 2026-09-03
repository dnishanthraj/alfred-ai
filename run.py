#!/usr/bin/env python3
"""
Entry point.

    python run.py                  # web console at http://127.0.0.1:8420
    python run.py --cli            # terminal console
    python run.py --contact lucius # connect to a specific contact
    python run.py --list           # show the directory
"""
import argparse
import sys
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="WayneTech console")
    parser.add_argument("--cli", action="store_true",
                        help="run the terminal console instead of the web console")
    parser.add_argument("--contact", default=None,
                        help="contact to connect to (default: alfred)")
    parser.add_argument("--list", action="store_true", help="list known contacts and exit")
    parser.add_argument("--no-open", action="store_true", help="don't open a browser window")
    args = parser.parse_args()

    from wayne import config
    from wayne.contacts import directory

    if args.list:
        for contact in directory():
            mark = " " if contact.availability.is_available() else "!"
            print(f" {mark} {contact.id:10} {contact.full_name:24} {contact.role}")
        return 0

    if args.cli:
        from wayne.frontends.cli import main as cli_main
        return cli_main(args.contact)

    from wayne.frontends.web import serve

    url = f"http://{config.WEB_HOST}:{config.WEB_PORT}"
    print(f"WayneTech console → {url}")
    print("Press Ctrl+C to shut down.\n")

    if not args.no_open:
        # Give uvicorn a moment to bind before the browser asks for the page.
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()

    try:
        serve()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
