"""Shared on-disk helpers for contact memory."""
import os
import tempfile


def atomic_write(path, text):
    """
    Write via a temp file in the same directory, then rename.

    A crash partway through a plain open(...,'w') truncates the file, and since
    a corrupt history is treated as "no history", that silently wipes the
    conversation memory.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
