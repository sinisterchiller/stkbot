"""SQLite storage layer for stock price reminders.

Provides a thin CRUD API over a SQLite database so the Discord bot can
persist pending price alerts across restarts.  Every public method opens
its own short-lived connection, so the store is safe to call from any
thread without sharing a global connection handle.

Default database path is ``reminders.db`` next to this file
(``Path(__file__).with_name("reminders.db")``), which is predictable under
systemd regardless of the working directory.  Tests inject a temporary
path via the ``db_path`` constructor argument.
"""

from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --- schema ----------------------------------------------------------------

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    target_price REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
    created_at TEXT NOT NULL
);
"""

# --- helpers ----------------------------------------------------------------


def _validate_target_price(value) -> float:
    """Return *value* as a positive finite float, raising ValueError otherwise."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"target_price must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"target_price must be positive and finite, got {value!r}")
    return number


def _validate_direction(direction: str) -> str:
    """Return *direction* when it is 'above' or 'below', else raise ValueError."""
    if direction not in ("above", "below"):
        raise ValueError(
            f"direction must be 'above' or 'below', got {direction!r}"
        )
    return direction


def _validate_ticker(ticker: str) -> str:
    """Return the stripped, upper-cased ticker; raise ValueError when empty."""
    cleaned = ticker.strip().upper()
    if not cleaned:
        raise ValueError("ticker must be a non-empty string")
    return cleaned


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict for caller convenience."""
    return dict(row)


# --- file permission hardening ------------------------------------------------

# Owner-only mode for the database file and its SQLite sidecars.  The
# pending alerts store user IDs, channel IDs and private alert targets,
# so none of these files may be world-readable (the default umask 022
# would leave them at 0644).
_DB_FILE_MODE = 0o600

# SQLite sidecar files that can contain the same alert data as the
# database file itself.
_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _tighten_file_mode(path: Path) -> None:
    """Chmod *path* to owner-only 0600, failing loudly when impossible.

    Refuses symlinked paths (the target may live outside the checkout)
    and raises RuntimeError when the mode cannot be set (for example
    when the file is owned by another user).
    """
    if path.is_symlink():
        raise RuntimeError(
            f"refusing to restrict a symlinked database path: {path}"
        )
    try:
        os.chmod(path, _DB_FILE_MODE)
    except OSError as exc:
        raise RuntimeError(
            f"cannot restrict {path} to owner-only 0600 (the file may be "
            f"owned by another user): {exc}"
        ) from exc


def _prepare_db_file(db_path: Path) -> None:
    """Ensure the database file exists with owner-only 0600 mode.

    A missing file is created empty *before* sqlite3 opens it, so the
    file never appears on disk with the process umask's default
    permissions.  An existing file is tightened in place; its contents
    are never touched, so pending alerts survive the upgrade.
    """
    if db_path.is_symlink():
        raise RuntimeError(
            f"refusing to open a symlinked database path: {db_path}"
        )
    if db_path.exists():
        _tighten_file_mode(db_path)
    else:
        fd = os.open(str(db_path), os.O_CREAT | os.O_WRONLY, _DB_FILE_MODE)
        os.close(fd)
        # Explicit chmod: the mode passed to os.open is masked by the
        # process umask, which is exactly the race we are avoiding.
        _tighten_file_mode(db_path)


def _tighten_sidecar_files(db_path: Path) -> None:
    """Best-effort 0600 on any SQLite sidecar files present on disk.

    Sidecars are transient (SQLite removes -wal/-shm on the last close)
    and are re-checked on every connection, so a sidecar created with
    the process umask during an operation is re-tightened by the next
    open.  Symlinked sidecars are skipped rather than followed, since
    their target may lie outside the checkout.
    """
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.is_symlink() or not sidecar.exists():
            continue
        try:
            os.chmod(sidecar, _DB_FILE_MODE)
        except OSError:
            # Sidecars are transient; a file we cannot chmod (for example
            # one owned by another user) is left for the next open.
            pass


# --- store ------------------------------------------------------------------


class ReminderStore:
    """SQLite-backed CRUD store for pending price reminders.

    Parameters
    ----------
    db_path : str | Path, optional
        Path to the SQLite database file.  Defaults to ``reminders.db``
        in the same directory as this module.  Tests should pass a
        ``tmp_path`` value here.
    """

    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        if db_path is None:
            db_path = Path(__file__).with_name("reminders.db")
        self._db_path = Path(db_path)

    # -- connection helper ---------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection with row access by column name.

        On POSIX the database file is tightened to owner-only 0600
        *before* the connection is made, so no WAL sidecar can be
        created on top of a world-readable database file, and any
        sidecar files already present are re-tightened after the
        connection opens.  On non-POSIX platforms the mode bits do not
        map to the same permissions, so no hardening is applied.
        """
        if os.name == "posix":
            _prepare_db_file(self._db_path)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        if os.name == "posix":
            _tighten_sidecar_files(self._db_path)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create the reminders table if it does not exist yet."""
        conn.execute(_SCHEMA)
        conn.commit()

    # -- CRUD ----------------------------------------------------------------

    def add_reminder(
        self,
        user_id: int,
        channel_id: int,
        ticker: str,
        target_price,
        direction: str,
    ) -> int:
        """Insert a pending reminder and return its new integer ID.

        Raises
        ------
        ValueError
            If *ticker*, *target_price*, or *direction* is invalid.
        """
        clean_ticker = _validate_ticker(ticker)
        price = _validate_target_price(target_price)
        clean_direction = _validate_direction(direction)
        now = datetime.now(timezone.utc).isoformat()

        conn = self._connect()
        try:
            self._ensure_schema(conn)
            cursor = conn.execute(
                "INSERT INTO reminders (user_id, channel_id, ticker, target_price, direction, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, channel_id, clean_ticker, price, clean_direction, now),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]
        finally:
            conn.close()

    def list_by_user(self, user_id: int) -> list[dict]:
        """Return all pending reminders belonging to *user_id*."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT id, user_id, channel_id, ticker, target_price, direction, created_at "
                "FROM reminders WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def list_all(self) -> list[dict]:
        """Return every pending reminder, ordered by ID."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT id, user_id, channel_id, ticker, target_price, direction, created_at "
                "FROM reminders ORDER BY id"
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    def get_by_id(self, reminder_id: int) -> Optional[dict]:
        """Return a single reminder by ID, or None when not found."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT id, user_id, channel_id, ticker, target_price, direction, created_at "
                "FROM reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            return _row_to_dict(row) if row is not None else None
        finally:
            conn.close()

    def delete_by_id(self, reminder_id: int, user_id: int) -> bool:
        """Delete a reminder **only** when the invoking *user_id* owns it.

        Returns True when a row was deleted, False when the ID is unknown
        or belongs to a different user (cross-user cancellation is prevented).
        """
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            cursor = conn.execute(
                "DELETE FROM reminders WHERE id = ? AND user_id = ?",
                (reminder_id, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_by_id_only(self, reminder_id: int) -> bool:
        """Delete a reminder by ID without a user check.

        Intended for post-notification cleanup where the checker already
        verified ownership.  Returns True when a row was deleted.
        """
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            cursor = conn.execute(
                "DELETE FROM reminders WHERE id = ?",
                (reminder_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
