"""A day's OMNIA conversation is named like any other chat: "Nov. 7"."""

from datetime import date

from app.overseer.threads import ThreadStore, daily_thread_title
from app.vault.internal import atomic_json, read_json


def test_title_is_the_short_day():
    assert daily_thread_title(date(2026, 11, 7)) == "Nov. 7"
    assert daily_thread_title(date(2026, 1, 28)) == "Jan. 28"
    # A whole month name takes no period.
    assert daily_thread_title(date(2026, 5, 3)) == "May 3"


def test_ensure_daily_names_the_thread(tmp_path):
    store = ThreadStore(tmp_path)
    thread, created = store.ensure_daily("2026-11-28")
    assert created
    assert thread["id"] == "daily-2026-11-28"
    assert thread["kind"] == "daily"
    assert thread["title"] == "Nov. 28"
    again, created = store.ensure_daily("2026-11-28")
    assert not created and again["id"] == thread["id"]


def test_old_long_titles_are_renamed_once(tmp_path):
    store = ThreadStore(tmp_path)
    old, _ = store.ensure_daily("2026-11-07")
    renamed, _ = store.ensure_daily("2026-11-08")
    for thread, title in ((old, "Saturday, November 7"), (renamed, "Groceries")):
        path = store._path(thread["id"])
        value = read_json(path, None)
        value["title"] = title
        value["updated_at"] = "2026-11-08T09:00:00Z"
        atomic_json(path, value)

    assert store.retitle_daily_threads() == 1
    after = {t["id"]: t for t in store.list()}
    assert after["daily-2026-11-07"]["title"] == "Nov. 7"
    # Renaming is not a change to the conversation: the rail order holds.
    assert after["daily-2026-11-07"]["updated_at"] == "2026-11-08T09:00:00Z"
    # A name the user chose stays.
    assert after["daily-2026-11-08"]["title"] == "Groceries"
    assert store.retitle_daily_threads() == 0
