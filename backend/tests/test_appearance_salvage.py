"""A stored appearance that no longer validates is salvaged, not reset.

A read that answered `configured: false` made the layout PUT this device's
localStorage copy over the server's saved themes, and OMNIA's appearance tools
wrote whatever defaults the read fell back to. The likeliest trigger is a new
THEME_COLOR_VARS entry, which every theme saved before it lacks — so missing
colours are backfilled, and only structurally broken entries are dropped.
"""
import asyncio
import logging

from app.api import meta as meta_api
from app.overseer.tool_expansion import (
    get_appearance,
    manage_saved_theme,
    set_appearance,
)
from app.vault.internal import atomic_json, read_json
from test_task_boards_api import make_client


def palette(mode: str = "dark", fill: str = "#123456", *, without=()) -> dict:
    colors = {
        name: fill for name in meta_api.THEME_COLOR_VARS if name not in without
    }
    return {"mode": mode, "colors": colors}


def config_path(state):
    return state.settings.vault_dir / ".exscalia" / "config.json"


def store(state, appearance: dict) -> None:
    atomic_json(config_path(state), {"timezone": "UTC", "appearance": appearance})


def stored_appearance(state) -> dict:
    return read_json(config_path(state), {})["appearance"]


def missing_var_config() -> dict:
    return {
        "theme": "custom",
        "font": "serif",
        "intoxicate": False,
        "custom_theme": palette(),
        "saved_themes": [
            {"id": "midnight", "name": "Midnight", "theme": palette()},
            # Saved before `removed` existed.
            {"id": "old", "name": "Old", "theme": palette(without=("removed",))},
        ],
        "active_saved_theme_id": "midnight",
    }


def test_get_backfills_a_saved_theme_missing_a_colour(state, caplog):
    store(state, missing_var_config())
    client = make_client(state, meta_api.router)

    with caplog.at_level(logging.WARNING, logger="app.api.meta"):
        body = client.get("/api/settings/appearance").json()

    assert body["configured"] is True
    assert body["backfilled"] == 1
    assert "dropped" not in body
    assert [theme["id"] for theme in body["saved_themes"]] == ["midnight", "old"]
    old = body["saved_themes"][1]["theme"]["colors"]
    assert old["removed"] == "#888888"
    assert old["bg"] == "#123456"
    assert body["theme"] == "custom" and body["font"] == "serif"
    assert body["active_saved_theme_id"] == "midnight"
    assert "appearance config salvaged: dropped=0 backfilled=1" in caplog.text

    # The client echoes the read back on its next save; strict validation
    # takes the backfilled palette, extra keys and all.
    saved = client.put("/api/settings/appearance", json=body)
    assert saved.status_code == 200
    assert stored_appearance(state)["saved_themes"][1]["theme"]["colors"][
        "removed"
    ] == "#888888"


def test_get_drops_only_the_malformed_saved_theme(state):
    appearance = missing_var_config()
    appearance["saved_themes"].append(
        {"id": "broken", "name": "Broken", "theme": "not a palette"}
    )
    store(state, appearance)
    client = make_client(state, meta_api.router)

    body = client.get("/api/settings/appearance").json()
    assert body["configured"] is True
    assert body["dropped"] == 1
    assert [theme["id"] for theme in body["saved_themes"]] == ["midnight", "old"]
    # The active theme survived, so it stays active.
    assert body["active_saved_theme_id"] == "midnight"

    appearance["active_saved_theme_id"] = "broken"
    store(state, appearance)
    body = client.get("/api/settings/appearance").json()
    assert body["active_saved_theme_id"] is None
    assert [theme["id"] for theme in body["saved_themes"]] == ["midnight", "old"]


def test_duplicate_ids_keep_the_first_theme(state):
    appearance = missing_var_config()
    appearance["saved_themes"].append(
        {"id": "midnight", "name": "Copy", "theme": palette()}
    )
    store(state, appearance)

    body = make_client(state, meta_api.router).get("/api/settings/appearance").json()
    assert body["dropped"] == 1
    assert [theme["name"] for theme in body["saved_themes"]] == ["Midnight", "Old"]


def test_an_unknown_theme_value_falls_back_alone(state):
    appearance = missing_var_config()
    appearance["theme"] = "neon"
    store(state, appearance)

    body = make_client(state, meta_api.router).get("/api/settings/appearance").json()
    assert body["configured"] is True
    assert body["theme"] == "system"
    assert body["font"] == "serif"
    assert len(body["saved_themes"]) == 2
    assert body["dropped"] == 1


def test_put_still_rejects_a_palette_missing_a_colour(state):
    client = make_client(state, meta_api.router)
    result = client.put(
        "/api/settings/appearance",
        json={"theme": "custom", "custom_theme": palette(without=("removed",))},
    )
    assert result.status_code == 400


def test_no_stored_block_is_still_unconfigured(state):
    body = make_client(state, meta_api.router).get("/api/settings/appearance").json()
    assert body["configured"] is False
    assert body["saved_themes"] == []
    assert "dropped" not in body and "backfilled" not in body


def test_omnia_theme_switch_keeps_every_saved_theme(acting_state):
    state = acting_state
    store(state, missing_var_config())

    read = asyncio.run(get_appearance(state, "t", {}))
    assert read["configured"] is True
    assert read["backfilled"] == 1

    asyncio.run(set_appearance(state, "t", {"theme": "dark"}))
    stored = stored_appearance(state)
    assert stored["theme"] == "dark"
    assert [theme["id"] for theme in stored["saved_themes"]] == ["midnight", "old"]
    assert stored["saved_themes"][1]["theme"]["colors"]["removed"] == "#888888"
    assert read_json(config_path(state), {})["timezone"] == "UTC"


def test_omnia_applies_a_backfilled_theme(acting_state):
    state = acting_state
    store(state, missing_var_config())

    asyncio.run(
        manage_saved_theme(state, "t", {"operation": "apply", "theme_id": "old"})
    )
    stored = stored_appearance(state)
    assert stored["theme"] == "custom"
    assert stored["active_saved_theme_id"] == "old"
    assert stored["custom_theme"]["colors"]["removed"] == "#888888"
    assert len(stored["saved_themes"]) == 2


def test_omnia_write_logs_what_it_drops(acting_state, caplog):
    state = acting_state
    appearance = missing_var_config()
    appearance["saved_themes"].append({"id": "", "name": "Nameless id", "theme": palette()})
    store(state, appearance)

    with caplog.at_level(logging.WARNING, logger="app.overseer.tool_expansion"):
        asyncio.run(set_appearance(state, "t", {"font": "mono"}))
    assert "set_appearance writes appearance without 1 unusable stored entries" in caplog.text
    stored = stored_appearance(state)
    assert stored["font"] == "mono"
    assert [theme["id"] for theme in stored["saved_themes"]] == ["midnight", "old"]
