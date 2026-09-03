"""
On-disk helpers for contact memory: atomic writes, and encryption at rest.

Memory is the most personal thing this project stores — a running transcript
plus a vault of facts someone deliberately confided. Left in plaintext it is
readable by anything that can read the home directory. When `WAYNE_MEMORY_KEY`
is set, both are encrypted with Fernet (AES-128-CBC with an HMAC), transparently
to every caller.

The key lives in `.env`, on the same disk as the ciphertext. That is worth
being clear about: this protects against casual reading, backups, sync clients
and other people's processes — not against someone who already has your `.env`.
Doing better means a passphrase typed at each start, which for a console you
talk to hands-free is a worse trade.
"""
import os
import tempfile

from .. import config

_fernet = None
_fernet_failed = False


def _cipher():
    """The Fernet instance, or None when no key is configured."""
    global _fernet, _fernet_failed
    if _fernet is not None or _fernet_failed:
        return _fernet
    if not config.MEMORY_KEY:
        _fernet_failed = True
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(config.MEMORY_KEY.encode())
    except Exception:
        # A malformed key must not take memory down with it; fall back to
        # plaintext rather than losing the ability to read anything.
        _fernet_failed = True
        return None
    return _fernet


def generate_key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def encrypt(text):
    cipher = _cipher()
    if cipher is None:
        return text.encode()
    return cipher.encrypt(text.encode())


def _looks_encrypted(raw):
    """
    Fernet tokens are urlsafe-base64 of a payload whose first byte is 0x80,
    which always renders as the prefix below.

    This check matters more than it looks: a token is valid ASCII, so decoding
    one as text *succeeds* and hands back base64 that would then be stored as
    if it were a conversation. Undecryptable ciphertext has to be recognised
    rather than merely failing to parse.
    """
    return raw[:6] == b"gAAAAA"


def decrypt(raw):
    """
    Read memory back, whether or not it is encrypted.

    Files written before a key was configured are still plaintext, so a blob
    that isn't a token is read as UTF-8 — turning encryption on must not look
    like amnesia. A token we cannot open yields empty rather than garbage.
    """
    if not raw:
        return ""

    cipher = _cipher()
    if cipher is not None:
        try:
            return cipher.decrypt(raw).decode()
        except Exception:
            pass

    if _looks_encrypted(raw):
        # Encrypted with a key we no longer have, or none is configured.
        # Better an empty memory than ciphertext masquerading as content.
        return ""

    try:
        return raw.decode()
    except UnicodeDecodeError:
        return ""


def atomic_write(path, text):
    """
    Write via a temp file in the same directory, then rename.

    A crash partway through a plain open(...,'w') truncates the file, and since
    a corrupt history is treated as "no history", that silently wipes the
    conversation memory.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".swap")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encrypt(text))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path):
    """Read a memory file, decrypting if necessary. '' when absent."""
    try:
        with open(path, "rb") as f:
            return decrypt(f.read())
    except FileNotFoundError:
        return ""
