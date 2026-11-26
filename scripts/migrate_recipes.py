#!/usr/bin/env python3
"""Move the standalone recipe app's recipes into EXSCALIA. A one-off, run by hand.

    python3 scripts/migrate_recipes.py --db <recipes.db> --url https://notes.exscalia.art --dry-run
    python3 scripts/migrate_recipes.py --db <recipes.db> --url https://notes.exscalia.art

Reads a copy of the old SQLite file (one table, `recipes`, the record as JSON in
`data`, menu order in `position`), maps each record to the RecipeIn shape of
backend/app/recipe_models.py, uploads a `data:` thumbnail as an attachment,
creates the recipe with POST /api/recipes, and finally saves the old menu order
with PUT /api/recipes/order. Drawings travel with the record, so nothing is
redrawn. A recipe whose title already exists on the server is skipped, so a
rerun after a failure picks up where the last run stopped.

When the server has auth on, the password is asked for once (never an
argument, never echoed); a refused sign-in stops the script without retrying,
because the app locks sign-in after a few failures.

--dry-run writes nothing: it validates every record and prints what a real run
would do. With --url it also signs in (if needed) and reads the existing
recipes to tell creates from skips; without --url it stays offline.

Standard library only, and it must run under macOS's system python3 (3.9).
"""

import argparse
import base64
import binascii
import getpass
import ipaddress
import json
import math
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from collections import namedtuple
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

# -- the RecipeIn shape (backend/app/recipe_models.py) ---------------------------

UNITS = (
    "", "tsp", "tbsp", "cup", "ml", "l", "g", "kg", "oz", "lb",
    "pinch", "clove", "slice", "can", "bunch", "stick", "sprig",
)
SCALINGS = ("linear", "whole", "to_taste")
SOURCE_TYPES = (
    "typed", "text", "website", "photo", "dish_photo", "youtube", "reel", "tiktok", "video",
)
MAX_TITLE = 200
MAX_ID = 64
MAX_ART = 4000
DEFAULT_SERVINGS = 2

# Spellings a hand-edited record might carry; anything else unknown is kept in
# the item text ("1 handful basil") rather than lost.
UNIT_ALIASES = {
    "teaspoon": "tsp", "tablespoon": "tbsp", "tbs": "tbsp", "tbl": "tbsp",
    "millilitre": "ml", "milliliter": "ml", "litre": "l", "liter": "l",
    "gram": "g", "gr": "g", "kilogram": "kg", "kilo": "kg",
    "ounce": "oz", "pound": "lb", "lbs": "lb",
}

# backend/app/api/attachments.py: these extensions, at most 25 MB.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
IMAGE_TYPES = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp", "image/avif": ".avif",
}

# backend/app/api/auth.py: SessionAuth names its user "owner" unless
# OMNIA_AUTH_USERNAME says otherwise, and the session rides in this cookie.
DEFAULT_USERNAME = "owner"
SESSION_COOKIE = "__Host-omnia_session"
USER_AGENT = "exscalia-migrate-recipes/1"
TIMEOUT = 60

_ID_RE = re.compile(r"[^\w]+")
_DATA_URL = re.compile(r"^data:([^,]*?),(.*)$", re.S)


class InvalidRecord(ValueError):
    """An old record that cannot become a recipe at all."""


def _text(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    return None


def _int(value: Any, lo: int, hi: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    try:
        n = int(value)
    except ValueError:
        return None
    if n < lo or (hi is not None and n > hi):
        return None
    return n


def _amount(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        n = float(value)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def derived_id(item: str) -> str:
    """The id the server derives for an ingredient that has none."""
    return _ID_RE.sub("-", item.casefold()).strip("-")[:MAX_ID] or "item"


def _unit(raw: Any, item: str) -> Tuple[str, str, Optional[str]]:
    """(unit, item, warning). An unknown unit moves into the item text."""
    text = (_text(raw) or "").strip()
    key = text.lower().rstrip(".")
    if key in UNITS:
        return key, item, None
    singular = key[:-1] if key.endswith("s") else key
    if singular in UNITS:
        return singular, item, None
    alias = UNIT_ALIASES.get(key) or UNIT_ALIASES.get(singular)
    if alias:
        return alias, item, None
    return "", f"{text} {item}", f'unit "{text}" moved into the item'


def _ingredient(raw: Any, n: int, warnings: List[str]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        warnings.append(f"ingredient {n} dropped: not an object")
        return None
    item = (_text(raw.get("item")) or "").strip()
    if not item:
        warnings.append(f"ingredient {n} dropped: no item")
        return None
    ident = (_text(raw.get("id")) or "").strip()[:MAX_ID] or derived_id(item)
    amount = _amount(raw.get("amount"))
    if amount is None and raw.get("amount") is not None:
        warnings.append(f'ingredient "{item}": amount {raw.get("amount")!r} dropped')
    unit, item, warning = _unit(raw.get("unit"), item)
    if warning:
        warnings.append(warning)
    scaling = raw.get("scaling")
    if scaling not in SCALINGS:
        if scaling is not None:
            warnings.append(f'ingredient "{item}": scaling {scaling!r} read as linear')
        scaling = "linear"
    group = _text(raw.get("group"))
    return {
        "id": ident,
        "amount": amount,
        "unit": unit,
        "item": item,
        "prep": _text(raw.get("prep")) or "",
        "scaling": scaling,
        "estimated": raw.get("estimated") is True,
        "group": group if group and group.strip() else None,
    }


def _step(raw: Any, n: int, warnings: List[str]) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        text = _text(raw.get("text")) or ""
        timer_raw = raw.get("timer_seconds")
    else:
        text, timer_raw = (raw if isinstance(raw, str) else ""), None
    if not text.strip():
        warnings.append(f"step {n} dropped: no text")
        return None
    timer = _int(timer_raw, 1)
    if timer is None and timer_raw is not None:
        warnings.append(f"step {n}: timer {timer_raw!r} dropped")
    return {"text": text, "timer_seconds": timer}


def _tags(value: Any, warnings: List[str]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, list):
        raw = [t for t in value if isinstance(t, str)]
        if len(raw) != len(value):
            warnings.append("tags that were not text dropped")
    else:
        warnings.append("tags dropped: not a list")
        return []
    return sorted({t.strip().lower() for t in raw if t.strip()})


def _rows(value: Any) -> Optional[str]:
    """A drawing grid as one string; None when it is not text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(row, str) for row in value):
        return "\n".join(value)
    return None


def decode_data_url(url: str) -> Tuple[bytes, str]:
    """The bytes and the file extension of an image `data:` URL.
    Raises ValueError for anything the attachment route would not take."""
    match = _DATA_URL.match(url.strip())
    if not match:
        raise ValueError("not a data: URL")
    params = [p.strip().lower() for p in match.group(1).split(";")]
    payload = match.group(2)
    if "base64" in params[1:]:
        compact = re.sub(r"\s+", "", payload)
        compact += "=" * (-len(compact) % 4)
        try:
            data = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("not valid base64") from None
    else:
        data = urlparse.unquote_to_bytes(payload)
    if not data:
        raise ValueError("empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("larger than 25 MB")
    ext = IMAGE_TYPES.get(params[0]) or _sniff(data)
    if not ext:
        raise ValueError(f"not a JPEG, PNG, GIF, WebP or AVIF image ({params[0] or 'no type'})")
    return data, ext


def _sniff(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    return None


def _source(value: Any, warnings: List[str]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        warnings.append("source dropped: not an object")
        return None
    kind = value.get("type")
    if kind not in SOURCE_TYPES:
        if kind is not None:
            warnings.append(f"source type {kind!r} read as typed")
        kind = "typed"
    url = (_text(value.get("url")) or "").strip() or None
    thumbnail = (_text(value.get("thumbnail")) or "").strip() or None
    if thumbnail:
        if thumbnail[:5].lower() == "data:":
            try:
                decode_data_url(thumbnail)
            except ValueError as exc:
                warnings.append(f"thumbnail dropped: {exc}")
                thumbnail = None
        elif not thumbnail.lower().startswith(("https://", "http://")):
            warnings.append("thumbnail dropped: not a data: or web URL")
            thumbnail = None
    return {"type": kind, "url": url, "thumbnail": thumbnail}


def to_recipe_in(record: Any, fallback_title: str = "") -> Tuple[Dict[str, Any], List[str]]:
    """One old record (the JSON in `recipes.data`) as a RecipeIn body, plus a
    note of everything that had to change on the way.

    Identity and timestamps (id, created_at, updated_at) stay behind. A value
    the new shape cannot hold degrades to its default, and a bad ingredient or
    step is dropped, the way the server reads a note. A `data:` thumbnail is
    checked but left in place; the caller uploads it. Raises InvalidRecord
    when there is no title anywhere."""
    if not isinstance(record, dict):
        raise InvalidRecord("the record is not a JSON object")
    warnings: List[str] = []

    title = _one_line(_text(record.get("title")) or "") or _one_line(fallback_title)
    if not title:
        raise InvalidRecord("no title")
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE].rstrip()
        warnings.append(f"title cut to {MAX_TITLE} characters")

    servings = _int(record.get("base_servings"), 1, 100)
    if servings is None:
        if record.get("base_servings") is not None:
            warnings.append(f"servings {record.get('base_servings')!r} read as {DEFAULT_SERVINGS}")
        servings = DEFAULT_SERVINGS

    minutes = {}
    for key in ("prep_minutes", "cook_minutes"):
        minutes[key] = _int(record.get(key), 0)
        if minutes[key] is None and record.get(key) is not None:
            warnings.append(f"{key} {record.get(key)!r} dropped")

    raw_ingredients = record.get("ingredients")
    if raw_ingredients is not None and not isinstance(raw_ingredients, list):
        warnings.append("ingredients dropped: not a list")
        raw_ingredients = []
    ingredients = [
        i for i in (_ingredient(raw, n, warnings) for n, raw in enumerate(raw_ingredients or [], 1))
        if i is not None
    ]

    raw_steps = record.get("steps")
    if raw_steps is not None and not isinstance(raw_steps, list):
        warnings.append("steps dropped: not a list")
        raw_steps = []
    steps = [s for s in (_step(raw, n, warnings) for n, raw in enumerate(raw_steps or [], 1)) if s is not None]

    art = _rows(record.get("art")) or ""
    colors = _rows(record.get("art_colors")) or ""
    palette_raw = record.get("art_palette")
    palette = (
        {k: v for k, v in palette_raw.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(palette_raw, dict) else {}
    )
    if len(art) > MAX_ART or len(colors) > MAX_ART:
        warnings.append(f"drawing dropped: over {MAX_ART} characters")
        art = ""
    if not art:
        colors, palette = "", {}

    return {
        "title": title,
        "base_servings": servings,
        "prep_minutes": minutes["prep_minutes"],
        "cook_minutes": minutes["cook_minutes"],
        "ingredients": ingredients,
        "steps": steps,
        "notes": _text(record.get("notes")) or "",
        "tags": _tags(record.get("tags"), warnings),
        "source": _source(record.get("source"), warnings),
        "art": art,
        "art_colors": colors,
        "art_palette": palette,
    }, warnings


# -- the old database -------------------------------------------------------------

OldRow = namedtuple("OldRow", "rowid id title data")


def read_old_recipes(db_path: str) -> List[OldRow]:
    """Every row of the old `recipes` table, in the old menu order: position
    ascending, ties newest row first. Rows an old database never numbered come
    after, newest edit first, as the old app numbered them at its next start.

    Reads a copy, so the original is never opened by SQLite: the old app kept
    its file in WAL mode, which a read-only connection cannot open once the
    app has closed it cleanly. The write-ahead log comes along if there is one."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"error: no such file: {path}")
    with tempfile.TemporaryDirectory(prefix="migrate-recipes-") as tmp:
        copy = Path(tmp) / "recipes.db"
        shutil.copyfile(path, copy)
        wal = path.with_name(path.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        conn = sqlite3.connect(str(copy))
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(recipes)")}
            if not {"id", "title", "data"} <= columns:
                raise SystemExit(f"error: {path} has no recipes table of the old app's shape")
            position = "position" if "position" in columns else "NULL"
            updated = "updated_at" if "updated_at" in columns else "''"
            rows = conn.execute(
                f"SELECT rowid, id, title, data, {position}, {updated} FROM recipes"
            ).fetchall()
        except sqlite3.Error as exc:
            raise SystemExit(f"error: cannot read {path}: {exc}") from None
        finally:
            conn.close()
    placed = sorted((r for r in rows if r[4] is not None), key=lambda r: (r[4], -r[0]))
    unplaced = sorted((r for r in rows if r[4] is None), key=lambda r: (r[5] or "", r[0]), reverse=True)
    return [OldRow(r[0], r[1], r[2] or "", r[3]) for r in placed + unplaced]


# -- the server -------------------------------------------------------------------


class ServerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

    def __str__(self) -> str:
        return f"{self.status}: {self.message}" if self.status else self.message


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _detail(raw: bytes) -> str:
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    text = raw.decode("utf-8", "replace").strip()
    return text[:200] or "no message"


class Server:
    """JSON over HTTP with the session cookie carried by hand: the cookie is
    `Secure`, which a cookie jar would not send to a plain-http scratch server."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cookie = None  # type: Optional[str]
        self._opener = urlrequest.build_opener(_NoRedirect)

    def call(self, method: str, path: str, body: Any = None, *, raw: Optional[bytes] = None,
             content_type: str = "application/json") -> Tuple[int, Any, Any]:
        """(status, parsed JSON or None, headers). Raises ServerError for a
        status of 300 or more, or when the server cannot be reached."""
        data = raw
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode("utf-8")
        req = urlrequest.Request(self.base + path, data=data, method=method)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        if self.cookie:
            req.add_header("Cookie", f"{SESSION_COOKIE}={self.cookie}")
        try:
            with self._opener.open(req, timeout=TIMEOUT) as resp:
                payload = resp.read()
                status, headers = resp.status, resp.headers
        except urlerror.HTTPError as exc:
            if 300 <= exc.code < 400:
                where = exc.headers.get("Location", "elsewhere")
                raise ServerError(exc.code, f"redirected to {where}; pass that as --url") from None
            raise ServerError(exc.code, _detail(exc.read())) from None
        except (urlerror.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ServerError(0, f"cannot reach {self.base}: {reason}") from None
        try:
            parsed = json.loads(payload.decode("utf-8")) if payload else None
        except ValueError:
            raise ServerError(status, f"answered {method} {path} with something that is not JSON") from None
        return status, parsed, headers

    def get(self, path: str) -> Any:
        return self.call("GET", path)[1]

    def upload(self, filename: str, data: bytes, media_type: str) -> Dict[str, Any]:
        boundary = "migrate-" + uuid.uuid4().hex
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        _status, body, _headers = self.call(
            "POST", "/api/attachments", raw=head + data + tail,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return body


def _loopback(base: str) -> bool:
    host = urlparse.urlsplit(base).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _session_cookie(headers: Any) -> Optional[str]:
    for header in headers.get_all("Set-Cookie") or []:
        name, _, rest = header.partition("=")
        if name.strip() == SESSION_COOKIE:
            value = rest.split(";", 1)[0].strip()
            if value:
                return value
    return None


def sign_in(server: Server, username: str, ask=getpass.getpass) -> bool:
    """Sign in when the server wants it. True when a session was opened.
    One attempt only: a refusal raises SystemExit."""
    status = server.get("/api/auth/status")
    if not isinstance(status, dict) or not status.get("auth_required"):
        return False
    if urlparse.urlsplit(server.base).scheme != "https" and not _loopback(server.base):
        raise SystemExit("error: the server wants a password; use an https:// --url to send it")
    try:
        password = ask(f"Password for {username} at {server.base}: ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\ncancelled") from None
    try:
        _status, _body, headers = server.call(
            "POST", "/api/auth/login", {"username": username, "password": password}
        )
    except ServerError as exc:
        if exc.status in (401, 429):
            reason = exc.message.rstrip(".")
            raise SystemExit(f"sign-in refused ({exc.status}): {reason}. Stopped.") from None
        raise
    token = _session_cookie(headers)
    if not token:
        raise SystemExit("error: signed in, but the server set no session cookie")
    server.cookie = token
    after = server.get("/api/auth/status")
    if not (isinstance(after, dict) and after.get("authenticated")):
        raise SystemExit("error: signed in, but the server does not accept the session")
    return True


def sign_out(server: Server) -> None:
    try:
        server.call("POST", "/api/auth/logout")
    except ServerError:
        pass


# -- the run ----------------------------------------------------------------------


class Plan:
    def __init__(self, row: OldRow):
        self.row = row
        self.body = None  # type: Optional[Dict[str, Any]]
        self.warnings = []  # type: List[str]
        self.invalid = None  # type: Optional[str]
        self.existing = None  # type: Optional[str]
        self.path = None  # type: Optional[str]

    @property
    def title(self) -> str:
        return self.body["title"] if self.body else (self.row.title or f"row {self.row.rowid}")

    def data_thumbnail(self) -> Optional[str]:
        source = (self.body or {}).get("source") or {}
        thumb = source.get("thumbnail")
        return thumb if thumb and thumb[:5].lower() == "data:" else None


def plan_rows(rows: List[OldRow]) -> List[Plan]:
    plans = []
    for row in rows:
        plan = Plan(row)
        try:
            record = json.loads(row.data) if isinstance(row.data, str) else None
            plan.body, plan.warnings = to_recipe_in(record, row.title)
        except ValueError as exc:  # InvalidRecord, or data that is not JSON
            plan.invalid = str(exc) if isinstance(exc, InvalidRecord) else "data is not JSON"
        plans.append(plan)
    return plans


def match_existing(plans: List[Plan], cards: List[Dict[str, Any]]) -> List[str]:
    """Pair each plan with an existing recipe of the same title, first come
    first served, so two old recipes with one title pair with two existing
    ones. Answers the existing paths no plan claimed, in display order."""
    by_title = {}  # type: Dict[str, List[str]]
    for card in cards:
        by_title.setdefault(_one_line(str(card.get("title", ""))), []).append(card["path"])
    for plan in plans:
        if plan.body is None:
            continue
        paths = by_title.get(plan.title)
        if paths:
            plan.existing = paths.pop(0)
    claimed = {p.existing for p in plans if p.existing}
    return [card["path"] for card in cards if card["path"] not in claimed]


def _say(*parts: str) -> None:
    print("  ".join(p for p in parts if p), flush=True)


def _notes(warnings: List[str]) -> str:
    return f"[{'; '.join(warnings)}]" if warnings else ""


def _thumbnail_name(title: str, ext: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-._")[:60].lower() or "recipe"
    return f"{stem}{ext}"


def migrate(server: Server, plans: List[Plan]) -> Tuple[int, int, int]:
    created = skipped = failed = 0
    for plan in plans:
        if plan.invalid:
            failed += 1
            _say("failed ", plan.title, f"(invalid record: {plan.invalid})")
            continue
        if plan.existing:
            skipped += 1
            plan.path = plan.existing
            _say("skipped", plan.title, f"(title already exists: {plan.existing})")
            continue
        body = json.loads(json.dumps(plan.body))
        thumb = plan.data_thumbnail()
        try:
            if thumb:
                data, ext = decode_data_url(thumb)
                media_type = {".jpg": "image/jpeg"}.get(ext, "image/" + ext[1:])
                try:
                    uploaded = server.upload(_thumbnail_name(plan.title, ext), data, media_type)
                except ServerError as exc:
                    raise ServerError(exc.status, f"thumbnail upload: {exc.message}") from None
                body["source"]["thumbnail"] = uploaded["url"]
            _status, recipe, _headers = server.call("POST", "/api/recipes", body)
        except ServerError as exc:
            failed += 1
            _say("failed ", plan.title, f"({exc})")
            continue
        plan.path = recipe["path"]
        created += 1
        _say("created", plan.title, "->", plan.path, _notes(plan.warnings))
    return created, skipped, failed


def save_order(server: Server, plans: List[Plan], others: List[str]) -> bool:
    """Recipes that were already there keep their place on top; the migrated
    ones follow in the old menu order."""
    migrated = [p.path for p in plans if p.path]
    if not migrated:
        return True
    try:
        _status, body, _headers = server.call("PUT", "/api/recipes/order", {"order": others + migrated})
    except ServerError as exc:
        _say("failed ", "menu order", f"({exc})")
        return False
    order = body.get("order") if isinstance(body, dict) else None
    wanted = set(migrated)
    if not isinstance(order, list) or [p for p in order if p in wanted] != migrated:
        _say("failed ", "menu order", "(the server answered a different order)")
        return False
    _say("ordered", f"menu order saved ({len(order)} recipes)")
    return True


def dry_run(plans: List[Plan], checked: bool) -> int:
    create = skip = invalid = 0
    for plan in plans:
        if plan.invalid:
            invalid += 1
            _say("invalid     ", plan.title, f"({plan.invalid})")
        elif plan.existing:
            skip += 1
            _say("would skip  ", plan.title, f"(title already exists: {plan.existing})")
        else:
            create += 1
            notes = list(plan.warnings)
            thumb = plan.data_thumbnail()
            if thumb:
                data, ext = decode_data_url(thumb)
                notes.insert(0, f"thumbnail {ext[1:]} {max(1, len(data) // 1024)} KB to upload")
            _say("would create", plan.title, _notes(notes))
    tail = "" if checked else " (offline: existing recipes not checked)"
    print(f"dry run: {create} to create, {skip} to skip, {invalid} invalid; nothing written{tail}")
    return 1 if invalid else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Move the standalone recipe app's recipes into EXSCALIA."
    )
    parser.add_argument("--db", required=True, help="the old app's SQLite file (recipes.db)")
    parser.add_argument("--url", help="EXSCALIA's base URL, e.g. https://notes.exscalia.art")
    parser.add_argument("--username", default=DEFAULT_USERNAME,
                        help=f"the account to sign in as (default: {DEFAULT_USERNAME})")
    parser.add_argument("--dry-run", action="store_true", help="validate and report; write nothing")
    args = parser.parse_args(argv)
    if not args.url and not args.dry_run:
        parser.error("--url is required unless --dry-run")
    if args.url and urlparse.urlsplit(args.url).scheme not in ("http", "https"):
        parser.error("--url must start with https:// (or http:// for a local server)")

    rows = read_old_recipes(args.db)
    print(f"{len(rows)} recipes in {args.db}", flush=True)
    plans = plan_rows(rows)

    server = Server(args.url) if args.url else None
    signed_in = False
    try:
        others = []  # type: List[str]
        if server:
            try:
                signed_in = sign_in(server, args.username)
                listing = server.get("/api/recipes")
            except ServerError as exc:
                raise SystemExit(f"error: {exc}") from None
            cards = listing.get("recipes", []) if isinstance(listing, dict) else []
            others = match_existing(plans, cards)
            who = f"signed in as {args.username}" if signed_in else "auth off"
            print(f"{server.base}: {who}, {len(cards)} recipes there", flush=True)
        if args.dry_run:
            return dry_run(plans, server is not None)
        created, skipped, failed = migrate(server, plans)
        if not save_order(server, plans, others):
            failed += 1
        print(f"done: {created} created, {skipped} skipped, {failed} failed")
        return 1 if failed else 0
    finally:
        if server and server.cookie:
            sign_out(server)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
