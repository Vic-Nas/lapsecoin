"""Single-instance guard using an OS-level advisory file lock.

Why a file lock instead of a PID file: a PID file can lie. If the app
crashes, the PID it wrote can later be reused by a completely unrelated
process, so a naive "is this PID alive?" check can wrongly refuse to
start (false positive) or wrongly let a second instance through (false
negative). An OS-level advisory lock (flock on Linux/macOS,
LockFileEx on Windows) doesn't have that problem: the kernel itself
holds the lock and releases it automatically the instant the owning
process exits for any reason, clean shutdown, crash, kill -9, power
loss to the process. Nothing needs to run on the way out, so there's
nothing to forget to clean up and nothing that can go stale.

Usage:
    lock = SingleInstanceLock(lock_path)
    if not lock.acquire():
        print("Already running (pid %d). Exiting." % lock.holder_pid())
        sys.exit(1)
    ...
    # lock is released automatically on process exit; lock.release()
    # is also safe to call explicitly (e.g. in a finally block).
"""

import logging
import os

log = logging.getLogger("ec.singleton")

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


class SingleInstanceLock:
    """Holds an exclusive, non-blocking OS lock on a small file for as
    long as this process is alive. `acquire()` returns False rather than
    raising if another live instance already holds it, that's the
    normal, expected outcome, not an error."""

    def __init__(self, lock_path):
        self.lock_path = lock_path
        self._fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.lock_path)) or ".", exist_ok=True)
        # Open (not truncate-create-exclusive): the file itself is just a
        # lock target, its *contents* are informational only and are
        # rewritten fresh below.
        fh = open(self.lock_path, "a+")
        try:
            if IS_WINDOWS:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    fh.close()
                    return False
            else:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    fh.close()
                    return False
        except Exception:
            # Any unexpected failure acquiring the lock (permissions,
            # missing syscall, etc.) should not be treated as "someone
            # else is running", fail open rather than blocking a
            # genuine normal start over a filesystem quirk.
            log.warning("[singleton] lock check failed unexpectedly, proceeding without it", exc_info=True)
            try:
                fh.close()
            except Exception:
                pass
            return True

        # Lock acquired: record our identity for diagnostics, then keep
        # the handle open (and referenced on self) for the life of the
        # process. Do NOT close it, closing releases the lock.
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()) + "\n")
        fh.flush()
        self._fh = fh
        return True

    def holder_pid(self):
        """Best-effort PID of whoever currently holds (or last held) the
        lock file, purely for a friendlier message. Never used to make
        the accept/reject decision itself."""
        try:
            with open(self.lock_path) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def release(self):
        if self._fh is None:
            return
        try:
            if IS_WINDOWS:
                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None
