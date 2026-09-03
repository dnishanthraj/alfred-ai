#!/usr/bin/env python3
"""
Entry point.

    python run.py                  # web console at http://127.0.0.1:8420
    python run.py --cli            # terminal console
    python run.py --contact lucius # connect to a specific contact
    python run.py --list           # show the directory
    python run.py --new-key        # generate a memory encryption key
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
    parser.add_argument("--new-key", action="store_true",
                        help="generate a memory encryption key to put in .env")
    args = parser.parse_args()

    if args.new_key:
        from wayne.memory.store import generate_key
        print("Add this to your .env, then restart:\n")
        print(f"  WAYNE_MEMORY_KEY={generate_key()}\n")
        print("Existing plaintext memory keeps working and is re-encrypted as it")
        print("is next written. Lose this key and the memory is unreadable.")
        return 0

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
