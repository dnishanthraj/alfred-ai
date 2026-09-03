"""Filesystem locations, anchored to the project root regardless of CWD."""
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

ENV_FILE = ROOT_DIR / ".env"
WEB_DIR = ROOT_DIR / "web"
PROFILE_DIR = PACKAGE_DIR / "contacts" / "profiles"

# Per-contact memory lives under data/<contact id>/. Everything in here is
# personal and gitignored.
DATA_DIR = ROOT_DIR / "data"

# Where single-contact memory lived before the console became a phone book.
# Kept only so it can be migrated into Alfred's namespace on first run.
LEGACY_HISTORY_FILE = ROOT_DIR / "batcomputer_history.json"
LEGACY_VAULT_FILE = ROOT_DIR / "batcomputer_vault.txt"


def contact_dir(contact_id):
    path = DATA_DIR / contact_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def history_file(contact_id):
    return contact_dir(contact_id) / "history.json"


def vault_file(contact_id):
    return contact_dir(contact_id) / "vault.txt"
