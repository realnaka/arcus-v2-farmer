"""Single-instance guard for the farmer.

The live bot is meant to run exactly one process per account config.  A wedged
but still-alive process (the bug this guards against) holds the dashboard port
and the SQLite file, so a fresh start dies on "address already in use" and
launchd's KeepAlive loops forever restarting it.  An advisory file lock makes
that failure explicit and clean: the second process refuses to start instead of
crashing on a socket bind.

The lock is an ``fcntl.flock`` on a file derived from the config's
``storage_path``, so different accounts and testnet vs mainnet get independent
locks.  flock is self-clearing - when the holding process exits (including a
crash), the lock is released automatically, so a restart always proceeds.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

_LOCK_OK = fcntl is not None


class SingleInstance:
    """Advisory lock preventing two live farmers for the same config."""

    def __init__(self, lock_path: str | Path) -> None:
        self.lock_path = Path(lock_path).expanduser()
        self._fh = None

    def acquire(self) -> bool:
        """Try to take the lock.

        Returns True when this process now owns the lock, False when another
        process (or another live instance) already holds it.  On non-POSIX
        platforms without ``fcntl`` this degrades to a no-op that always
        succeeds, because the farm targets macOS/Linux.
        """
        if not _LOCK_OK:  # pragma: no cover
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(self.lock_path, "a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            return False
        # Make the holder identifiable; the lock is advisory but a stale pid is
        # useful when debugging "why did my instance refuse to start".
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        self._fh = lock
        return True

    def release(self) -> None:
        """Drop the lock.  Safe to call more than once."""
        if self._fh is None:
            return
        fh, self._fh = self._fh, None
        try:
            if _LOCK_OK:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - already unlocked / closed
            pass
        finally:
            fh.close()

    @property
    def held(self) -> bool:
        return self._fh is not None
