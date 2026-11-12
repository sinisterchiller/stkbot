"""Focused storage tests for the reminders SQLite layer.

Uses pytest ``tmp_path`` for isolated databases; no network, no tokens.
"""

import sqlite3

import pytest

from reminders import ReminderStore, _validate_direction, _validate_target_price, _validate_ticker


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

    expected = {"id", "user_id", "channel_id", "ticker", "target_price", "direction", "created_at"}
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
