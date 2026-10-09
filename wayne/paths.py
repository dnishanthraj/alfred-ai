"""Filesystem locations, anchored to the project root regardless of CWD."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

ENV_FILE = ROOT_DIR / ".env"
WEB_DIR = ROOT_DIR / "web"
PROFILE_DIR = PACKAGE_DIR / "contacts" / "profiles"

# Per-contact memory lives under data/<contact id>/. Everything in here is
# personal and gitignored.
# WAYNE_DATA_DIR points it elsewhere — a sandbox to try things in without
# writing into anyone's real memories. .env is read here, not only in config:
# this module is imported first, and a sandbox set in .env that was decided
# before .env was loaded would quietly have written into the real thing.
load_dotenv(ENV_FILE)
DATA_DIR = Path(os.getenv("WAYNE_DATA_DIR") or ROOT_DIR / "data").expanduser()

# Where single-contact memory lived before the console became a phone book.
# Kept only so it can be migrated into Alfred's namespace on first run.
LEGACY_HISTORY_FILE = ROOT_DIR / "batcomputer_history.json"
LEGACY_VAULT_FILE = ROOT_DIR / "batcomputer_vault.txt"


def contact_dir(contact_id):
    """
    Where a contact's memory lives. Deliberately does not create the directory:
    merely naming a path shouldn't leave a folder behind, which is what made
    the test suite litter `data/` with every contact id it mentioned. The
    directory is created when something is actually written.
    """
    return DATA_DIR / contact_id


def history_file(contact_id):
    return contact_dir(contact_id) / "history.json"


def vault_file(contact_id):
    return contact_dir(contact_id) / "vault.txt"


def story_file(contact_id):
    """Facts established in the game he plays as Bruce — never real life."""
    return contact_dir(contact_id) / "story.txt"


def bio_file(contact_id):
    """Who this contact is to the operator, in the operator's own words."""
    return contact_dir(contact_id) / "bio.txt"
