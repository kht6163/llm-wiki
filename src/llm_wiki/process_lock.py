"""One cross-process lifecycle lock shared by serving and destructive restore."""
from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout


class ProjectLockError(RuntimeError):
    """Another process owns the database lifecycle lock."""


class ProjectLock:
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        self.path = db_path.parent / ".llm-wiki.lock"
        self._lock = FileLock(self.path)
        self._held = False

    def acquire(self) -> ProjectLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock.acquire(timeout=0)
        except Timeout as exc:
            raise ProjectLockError(
                f"another llm-wiki serve or restore process is already active: {self.path}"
            ) from exc
        self._held = True
        return self

    def release(self) -> None:
        if self._held:
            self._lock.release()
            self._held = False

    def __enter__(self) -> ProjectLock:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()
