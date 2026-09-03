"""Per-contact memory: recent conversation plus an explicit long-term vault."""
import json

from .. import paths
from .history import History
from .store import atomic_write, read_text
from .vault import Vault

__all__ = ["History", "Vault", "atomic_write", "read_text", "migrate_legacy"]


def migrate_legacy(contact_id):
    """
    Move single-contact memory into a contact namespace.

    Before the console became a phone book there was one history file and one
    vault at the project root. Losing an existing conversation to a refactor
    would be the worst kind of upgrade, so they are moved on first run and the
    originals renamed rather than deleted.
    """
    moved = []

    if paths.LEGACY_HISTORY_FILE.exists():
        target = paths.history_file(contact_id)
        if not target.exists():
            try:
                with open(paths.LEGACY_HISTORY_FILE) as f:
                    legacy = json.load(f)  # only migrate what we can read
                # Written through the store so it lands encrypted if a key is set.
                atomic_write(target, json.dumps(legacy, indent=2))
                moved.append("history")
            except (OSError, json.JSONDecodeError):
                pass
        paths.LEGACY_HISTORY_FILE.rename(
            paths.LEGACY_HISTORY_FILE.with_suffix(".json.migrated")
        )

    if paths.LEGACY_VAULT_FILE.exists():
        target = paths.vault_file(contact_id)
        if not target.exists():
            atomic_write(target, paths.LEGACY_VAULT_FILE.read_text())
            moved.append("vault")
        paths.LEGACY_VAULT_FILE.rename(
            paths.LEGACY_VAULT_FILE.with_suffix(".txt.migrated")
        )

    return moved
