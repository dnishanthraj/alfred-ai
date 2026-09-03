"""Filesystem locations, anchored to the project root regardless of CWD."""
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = ROOT_DIR / ".env"
MODELFILE = ROOT_DIR / "Modelfile"
HISTORY_FILE = ROOT_DIR / "batcomputer_history.json"
CORE_MEMORY_FILE = ROOT_DIR / "batcomputer_vault.txt"
WEB_DIR = ROOT_DIR / "web"
