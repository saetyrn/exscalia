"""The hub's Dailies: tasks that repeat every day, as today finds them."""
from datetime import date, timedelta

from app.index import parse
from app.tz import local_today
from tests.conftest import make_client
from app.api import tasks as tasks_api


def client_for(state):
    return make_client(state, tasks_api.router)


def test_a_daily_created_through_the_api_is_indexed_as_one(acting_state):
    client = client_for(acting_state)
    today = local_today(acting_state)
    made = client.post(
        "/api/tasks", json={"text": "Stretch", "repeats": "daily", "due": today}
    ).json()
    assert made["repeats"] == "daily"
    content = acting_state.store.read("Tasks Inbox.md")
    task = next(t for t in parse.extract_tasks(content) if t.id == made["id"])
    assert task.repeats == "daily"


def test_dailies_lists_open_and_done_today_but_not_tomorrows_copy(acting_state):
    client = client_for(acting_state)
    today = local_today(acting_state)
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    stretch = client.post(
        "/api/tasks", json={"text": "Stretch", "repeats": "daily", "due": today}
    ).json()
    read = client.post(
        "/api/tasks", json={"text": "Read 20 pages", "repeats": "daily", "due": today}
    ).json()
    client.post("/api/tasks", json={"text": "Weekly review", "repeats": "weekly", "due": today})
    client.post("/api/tasks", json={"text": "One-off", "due": today})
    client.post("/api/tasks", json={"text": "Later daily", "repeats": "daily", "due": tomorrow})

    listed = {t["text"]: t for t in client.get("/api/tasks/dailies").json()}
    assert set(listed) == {"Stretch", "Read 20 pages"}

    # Completing spawns tomorrow's copy; today shows the ticked one only.
    assert client.patch(f"/api/tasks/{read['id']}", json={"status": "done"}).status_code == 200
    listed = client.get("/api/tasks/dailies").json()
    reads = [t for t in listed if t["text"] == "Read 20 pages"]
    assert [(t["id"], t["done"]) for t in reads] == [(read["id"], True)]
    assert any(t["id"] == stretch["id"] and not t["done"] for t in listed)

    # Unticking brings it back without a second copy for tomorrow.
    client.patch(f"/api/tasks/{read['id']}", json={"status": "todo"})
    client.patch(f"/api/tasks/{read['id']}", json={"status": "done"})
    copies = [t for t in client.get("/api/tasks").json() if t["text"] == "Read 20 pages"]
    assert len([t for t in copies if t["due"] == tomorrow]) == 1


def test_an_unknown_repeat_is_refused(acting_state):
    response = client_for(acting_state).post(
        "/api/tasks", json={"text": "x", "repeats": "hourly"}
    )
    assert response.status_code == 400
