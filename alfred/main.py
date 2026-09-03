"""
Backwards-compatible entry point.

The conversation engine now lives in `alfred.core` and the terminal renderer in
`alfred.cli`. This module is kept so `from alfred.main import main` keeps
working; new code should import from one of those directly.
"""
from .cli import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
