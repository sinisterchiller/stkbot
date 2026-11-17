"""Focused storage tests for the reminders SQLite layer.

Uses pytest ``tmp_path`` for isolated databases; no network, no tokens.
"""

import os
import sqlite3
import stat

import pytest

from reminders import (
    ReminderStore,
    _validate_audience,
    _validate_direction,
    _validate_target_price,
    _validate_ticker,
)


# -- helpers -----------------------------------------------------------------


def _make_store(tmp_path) -> ReminderStore:
    """Create a ReminderStore backed by a temp database."""
    return ReminderStore(db_path=tmp_path / "reminders.db")


# -- schema ------------------------------------------------------------------


def test_schema_created(tmp_path):
    """The reminders table exists with the expected columns."""
    store = _make_store(tmp_path)
    # Any CRUD call creates the schema; verify via sqlite directly.
    store.add_reminder(1, 2, "NVDA", 100.0, "above")

    db = tmp_path / "reminders.db"
    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    finally:
        conn.close()

    expected = {
        "id", "user_id", "channel_id", "ticker", "target_price", "direction",
        "audience", "armed", "created_at",
    }
    assert expected.issubset(cols)


# -- persistence across reopened connections ---------------------------------


def test_persistence_across_reopened_store(tmp_path):
    """Data written by one store instance is visible to a new instance on the same file."""
    store1 = _make_store(tmp_path)
    rid = store1.add_reminder(1, 2, "NVDA", 100.0, "above")

    store2 = _make_store(tmp_path)
    rows = store2.list_by_user(1)
    assert len(rows) == 1
    assert rows[0]["id"] == rid
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["target_price"] == 100.0
    assert rows[0]["direction"] == "above"


# -- add_reminder --------------------------------------------------------------


def test_add_returns_increasing_ids(tmp_path):
    store = _make_store(tmp_path)
    id1 = store.add_reminder(1, 2, "NVDA", 100.0, "above")
    id2 = store.add_reminder(1, 2, "AAPL", 200.0, "below")
    assert id2 == id1 + 1


def test_add_stores_all_fields(tmp_path):
    store = _make_store(tmp_path)
    rid = store.add_reminder(42, 99, "nvda", 99.99, "below")
    row = store.get_by_id(rid)
    assert row is not None
    assert row["user_id"] == 42
    assert row["channel_id"] == 99
    assert row["ticker"] == "NVDA"
    assert row["target_price"] == 99.99
    assert row["direction"] == "below"
    assert row["created_at"] is not None
    # The audience defaults to 'owner', so existing callers are unchanged.
    assert row["audience"] == "owner"
    # The armed bit defaults to 1, so every new alert starts armed.
    assert row["armed"] == 1


def test_add_stores_everyone_audience(tmp_path):
    store = _make_store(tmp_path)
    rid = store.add_reminder(42, 99, "NVDA", 99.99, "below", "everyone")
    row = store.get_by_id(rid)
    assert row is not None
    assert row["audience"] == "everyone"
    # The audience is visible through every listing path.
    assert store.list_by_user(42)[0]["audience"] == "everyone"
    assert store.list_all()[0]["audience"] == "everyone"


def test_add_rejects_invalid_audience(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "NVDA", 100.0, "above", "sideways")
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "NVDA", 100.0, "above", "")
    # Nothing was inserted.
    assert store.list_all() == []


def test_add_rejects_invalid_target(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "NVDA", -5, "above")
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "NVDA", 0, "above")
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "NVDA", "abc", "above")
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "NVDA", float("nan"), "above")
    # Nothing was inserted.
    assert store.list_all() == []


def test_add_rejects_invalid_direction(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "NVDA", 100.0, "sideways")
    assert store.list_all() == []


def test_add_rejects_empty_ticker(tmp_path):
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add_reminder(1, 2, "   ", 100.0, "above")
    assert store.list_all() == []


# -- validation helpers (unit) -------------------------------------------------


def test_validate_target_price_accepts_positive():
    assert _validate_target_price(42) == 42.0
    assert _validate_target_price("3.14") == 3.14


def test_validate_target_price_rejects_non_positive():
    with pytest.raises(ValueError):
        _validate_target_price(0)
    with pytest.raises(ValueError):
        _validate_target_price(-1)
    with pytest.raises(ValueError):
        _validate_target_price(float("inf"))


def test_validate_direction_accepts_valid():
    assert _validate_direction("above") == "above"
    assert _validate_direction("below") == "below"


def test_validate_direction_rejects_invalid():
    with pytest.raises(ValueError):
        _validate_direction("sideways")


def test_validate_audience_accepts_valid():
    assert _validate_audience("owner") == "owner"
    assert _validate_audience("everyone") == "everyone"


def test_validate_audience_rejects_invalid():
    with pytest.raises(ValueError):
        _validate_audience("sideways")


def test_validate_ticker_strips_and_uppercases():
    assert _validate_ticker("nvda") == "NVDA"
    assert _validate_ticker("  aapl  ") == "AAPL"


def test_validate_ticker_rejects_empty():
    with pytest.raises(ValueError):
        _validate_ticker("")
    with pytest.raises(ValueError):
        _validate_ticker("   ")


# -- user isolation ------------------------------------------------------------


def test_user_isolation(tmp_path):
    """User A's reminders are invisible to user B."""
    store = _make_store(tmp_path)
    store.add_reminder(1, 10, "NVDA", 100.0, "above")
    store.add_reminder(2, 20, "AAPL", 200.0, "below")

    user1 = store.list_by_user(1)
    user2 = store.list_by_user(2)
    assert len(user1) == 1
    assert user1[0]["ticker"] == "NVDA"
    assert len(user2) == 1
    assert user2[0]["ticker"] == "AAPL"


# -- list_all ------------------------------------------------------------------


def test_list_all_returns_every_pending(tmp_path):
    store = _make_store(tmp_path)
    store.add_reminder(1, 10, "NVDA", 100.0, "above")
    store.add_reminder(2, 20, "AAPL", 200.0, "below")
    store.add_reminder(1, 10, "TSLA", 300.0, "above")

    all_rows = store.list_all()
    assert len(all_rows) == 3
    ids = [r["id"] for r in all_rows]
    assert ids == sorted(ids)


# -- list_by_channel (channel-scoped listing) --------------------------------


def test_list_by_channel_returns_only_everyone_alerts_in_that_channel(tmp_path):
    """The listing is scoped by channel ID and audience: owner alerts and
    other channels' alerts are excluded, in ID order."""
    store = _make_store(tmp_path)
    owner_id = store.add_reminder(1, 10, "NVDA", 100.0, "above")
    own_id = store.add_reminder(2, 10, "AAPL", 200.0, "below", "everyone")
    other_id = store.add_reminder(1, 20, "TSLA", 300.0, "above", "everyone")

    rows = store.list_by_channel(10)
    assert [row["id"] for row in rows] == [own_id]
    assert rows[0]["audience"] == "everyone"
    assert rows[0]["armed"] == 1
    # The owner alert and the other channel's alert are not listed.
    listed_ids = [row["id"] for row in rows]
    assert owner_id not in listed_ids
    assert other_id not in listed_ids


def test_list_by_channel_empty(tmp_path):
    store = _make_store(tmp_path)
    store.add_reminder(1, 10, "NVDA", 100.0, "above")  # owner alert only
    assert store.list_by_channel(10) == []
    assert store.list_by_channel(99) == []


# -- delete_channel_alert (atomic owner+channel+audience authorization) -------


def test_delete_channel_alert_owned_in_channel(tmp_path):
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above", "everyone")
    assert store.delete_channel_alert(rid, user_id=1, channel_id=10) is True
    assert store.get_by_id(rid) is None


def test_delete_channel_alert_cross_user_prevented(tmp_path):
    """A different user cannot delete the alert even from the same channel."""
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above", "everyone")
    assert store.delete_channel_alert(rid, user_id=2, channel_id=10) is False
    # The alert is untouched.
    assert store.get_by_id(rid) is not None


def test_delete_channel_alert_cross_channel_prevented(tmp_path):
    """The owner cannot delete the alert from a different channel."""
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above", "everyone")
    assert store.delete_channel_alert(rid, user_id=1, channel_id=20) is False
    assert store.get_by_id(rid) is not None


def test_delete_channel_alert_owner_audience_prevented(tmp_path):
    """Owner ('owner') alerts are one-shot and only /unremind may delete them;
    this path never touches them, even for the owner in the right channel."""
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above")
    assert store.delete_channel_alert(rid, user_id=1, channel_id=10) is False
    assert store.get_by_id(rid) is not None


def test_delete_channel_alert_unknown_id(tmp_path):
    store = _make_store(tmp_path)
    assert store.delete_channel_alert(9999, user_id=1, channel_id=10) is False


# -- set_armed (channel alert state) ------------------------------------------


def test_set_armed_disarms_and_rearms(tmp_path):
    """set_armed flips the persisted armed bit in both directions."""
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 2, "NVDA", 100.0, "above", "everyone")
    assert store.get_by_id(rid)["armed"] == 1

    assert store.set_armed(rid, False) is True
    row = store.get_by_id(rid)
    assert row["armed"] == 0
    # The bit is visible through every listing path.
    assert store.list_by_user(1)[0]["armed"] == 0
    assert store.list_all()[0]["armed"] == 0

    assert store.set_armed(rid, True) is True
    assert store.get_by_id(rid)["armed"] == 1


def test_set_armed_state_survives_reopened_store(tmp_path):
    """A disarmed bit written by one store instance is visible to a new one.

    This is what makes a disarmed channel alert stay disarmed across a bot
    restart.
    """
    store1 = _make_store(tmp_path)
    rid = store1.add_reminder(1, 2, "NVDA", 100.0, "above", "everyone")
    store1.set_armed(rid, False)

    store2 = _make_store(tmp_path)
    assert store2.get_by_id(rid)["armed"] == 0


def test_set_armed_unknown_id(tmp_path):
    store = _make_store(tmp_path)
    assert store.set_armed(9999, False) is False


# -- delete_by_id (owner check) -------------------------------------------------


def test_delete_by_id_owned(tmp_path):
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above")
    assert store.delete_by_id(rid, user_id=1) is True
    assert store.get_by_id(rid) is None


def test_delete_by_id_cross_user_prevented(tmp_path):
    """User B cannot delete User A's reminder."""
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above")
    assert store.delete_by_id(rid, user_id=2) is False
    # The reminder still exists.
    assert store.get_by_id(rid) is not None


def test_delete_by_id_unknown_id(tmp_path):
    store = _make_store(tmp_path)
    assert store.delete_by_id(9999, user_id=1) is False


# -- delete_by_id_only (post-notification) -------------------------------------


def test_delete_by_id_only(tmp_path):
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above")
    assert store.delete_by_id_only(rid) is True
    assert store.get_by_id(rid) is None


def test_delete_by_id_only_unknown_id(tmp_path):
    store = _make_store(tmp_path)
    assert store.delete_by_id_only(9999) is False


# -- get_by_id -------------------------------------------------------------------


def test_get_by_id_returns_none_when_missing(tmp_path):
    store = _make_store(tmp_path)
    assert store.get_by_id(42) is None


def test_get_by_id_returns_row(tmp_path):
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 10, "NVDA", 100.0, "above")
    row = store.get_by_id(rid)
    assert row is not None
    assert row["id"] == rid
    assert row["ticker"] == "NVDA"


# -- duplicate / idempotency -----------------------------------------------------


def test_no_duplicate_inserts_on_failed_validation(tmp_path):
    """Failed validation must not leave partial rows behind."""
    store = _make_store(tmp_path)
    with pytest.raises(ValueError):
        store.add_reminder(1, 10, "NVDA", -1, "above")
    with pytest.raises(ValueError):
        store.add_reminder(1, 10, "NVDA", 100.0, "bad")
    assert store.list_all() == []


# -- audience migration (backward compatibility) --------------------------------

_LEGACY_SCHEMA = """\
CREATE TABLE reminders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    target_price REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
    created_at TEXT NOT NULL
);
"""


def _create_legacy_db(tmp_path) -> str:
    """Create a database with the pre-audience schema and one alert.

    Simulates a deployment running the old bot: the reminders table exists
    without the audience column and holds a pending owner alert.
    """
    db = tmp_path / "reminders.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO reminders (user_id, channel_id, ticker, target_price, direction, created_at) "
            "VALUES (1, 2, 'NVDA', 100.0, 'above', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    return str(db)


def test_legacy_db_migrates_with_owner_default(tmp_path):
    """A pre-audience database (no audience, no armed) migrates in place.

    The audience column is added with the 'owner' default, so the existing
    alert keeps its owner-only notification behaviour, and the armed bit is
    added with the 1 (armed) default, so the alert stays armed. No data is
    lost.
    """
    _create_legacy_db(tmp_path)
    store = _make_store(tmp_path)

    rows = store.list_all()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["target_price"] == 100.0
    assert rows[0]["direction"] == "above"
    assert rows[0]["audience"] == "owner"
    assert rows[0]["armed"] == 1

    db = tmp_path / "reminders.db"
    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    finally:
        conn.close()
    assert "audience" in cols
    assert "armed" in cols


_AUDIENCE_SCHEMA = """\
CREATE TABLE reminders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    target_price REAL NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('above', 'below')),
    audience TEXT NOT NULL DEFAULT 'owner' CHECK (audience IN ('owner', 'everyone')),
    created_at TEXT NOT NULL
);
"""


def _create_audience_only_db(tmp_path) -> str:
    """Create a database with the post-audience, pre-armed schema.

    Simulates a deployment running the /remind-in-channel bot: the
    reminders table has the audience column but not the armed bit, and
    holds a pending channel alert.
    """
    db = tmp_path / "reminders.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_AUDIENCE_SCHEMA)
        conn.execute(
            "INSERT INTO reminders (user_id, channel_id, ticker, target_price, direction, audience, created_at) "
            "VALUES (1, 2, 'NVDA', 100.0, 'above', 'everyone', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    return str(db)


def test_audience_only_db_migrates_with_armed_default(tmp_path):
    """A database that has audience but no armed column migrates in place.

    The bit is added with the 1 (armed) default, so the existing channel
    alert stays armed and keeps its audience; no data is lost.
    """
    _create_audience_only_db(tmp_path)
    store = _make_store(tmp_path)

    rows = store.list_all()
    assert len(rows) == 1
    assert rows[0]["audience"] == "everyone"
    assert rows[0]["armed"] == 1

    db = tmp_path / "reminders.db"
    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    finally:
        conn.close()
    assert "armed" in cols


def test_migrated_db_accepts_new_audiences_and_reopens_idempotently(tmp_path):
    """After migration, new alerts keep their audience and reopening is safe."""
    _create_legacy_db(tmp_path)
    store1 = _make_store(tmp_path)
    rid = store1.add_reminder(2, 20, "AAPL", 200.0, "below", "everyone")

    store2 = _make_store(tmp_path)
    rows = store2.list_all()
    assert [row["id"] for row in rows] == [1, rid]
    assert rows[0]["audience"] == "owner"  # migrated legacy row
    assert rows[1]["audience"] == "everyone"


# -- file permission hardening (POSIX-only) ------------------------------------

# File mode hardening only has meaning on POSIX platforms; on Windows the
# mode bits do not map to the same permissions, so these tests are skipped
# there.
requires_posix = pytest.mark.skipif(
    os.name != "posix", reason="file mode hardening is POSIX-only"
)


def _file_mode(path) -> int:
    """Return the permission bits of *path* (e.g. 0o600)."""
    return stat.S_IMODE(os.stat(path).st_mode)


@requires_posix
def test_new_db_created_with_owner_only_mode(tmp_path):
    """A brand-new database file is created 0600, not umask-default 0644."""
    store = _make_store(tmp_path)
    rid = store.add_reminder(1, 2, "NVDA", 100.0, "above")
    assert _file_mode(tmp_path / "reminders.db") == 0o600
    # The data is still readable after the mode tightening.
    assert store.get_by_id(rid)["ticker"] == "NVDA"


@requires_posix
def test_existing_world_readable_db_tightened_without_data_loss(tmp_path):
    """A pre-existing 0644 database is upgraded to 0600 in place."""
    db = tmp_path / "reminders.db"
    store1 = _make_store(tmp_path)
    rid = store1.add_reminder(1, 2, "NVDA", 100.0, "above")
    # Simulate a database created before the hardening (umask 022).
    os.chmod(db, 0o644)

    store2 = _make_store(tmp_path)
    rows = store2.list_by_user(1)
    assert _file_mode(db) == 0o600
    assert len(rows) == 1
    assert rows[0]["id"] == rid
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["target_price"] == 100.0


@requires_posix
def test_wal_shm_sidecars_not_left_world_readable(tmp_path):
    """Sidecar files present on disk are re-tightened to 0600 on open."""
    db = tmp_path / "reminders.db"
    store = _make_store(tmp_path)
    store.add_reminder(1, 2, "NVDA", 100.0, "above")

    # Keep a second WAL connection open so -wal/-shm exist on disk, then
    # relax them to the umask-022 default to simulate the pre-hardening
    # state.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO reminders (user_id, channel_id, ticker, target_price, direction, created_at) "
            "VALUES (1, 2, 'AAPL', 200.0, 'below', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = db.with_name(db.name + suffix)
            if sidecar.exists():
                os.chmod(sidecar, 0o644)

        # The store's next open must re-tighten any sidecar present.
        rows = store.list_by_user(1)
        for suffix in ("-wal", "-shm"):
            sidecar = db.with_name(db.name + suffix)
            if sidecar.exists():
                assert _file_mode(sidecar) == 0o600
        # Persistence is intact through the tightened open.
        assert len(rows) == 2
    finally:
        conn.close()
