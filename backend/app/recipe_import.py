"""Recipe website import: fetch a page and read the schema.org Recipe most recipe sites embed.

Ported from the standalone recipe app. The server is public, so every hop of a
fetch (the first URL and each redirect) must pass
`media_lookup.public_https_refusal`: https only, and no host that resolves to
loopback, the LAN or the cloud metadata endpoint. Nothing here calls an AI; a
page without recipe data still comes back as text for the AI step to read.
"""
import asyncio
import html
import json
import math
import re
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from . import media_lookup
from .recipe_models import Draft, Ingredient, RecipeIn, Source, Step
from .recipe_parse import (
    MAX_TEXT,
    _duration_minutes,
    _timer_seconds,
    dedupe_ids,
    link_references,
    parse_ingredient_line,
)

# Recipe sites put bot walls in front of agents that do not look like a browser.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15"
)
REQUEST_TIMEOUT = 10.0  # each connect or read
PAGE_TIMEOUT = 20.0  # the whole fetch: every hop, the body and the parse
PAGE_MAX_BYTES = 5 * 1024 * 1024
PAGE_MAX_REDIRECTS = 5
PAGE_TYPES = {"text/html", "application/xhtml+xml", "text/plain"}
MAX_DURATION_MINUTES = 60 * 24 * 365
# Ingredient and step text the schema.org reader takes from one page, past
# which it cuts: a hostile page then costs what the longest paste does.
MAX_SCHEMA_TEXT = MAX_TEXT

# Link kinds the website reader does not handle; the route refuses them.
_VIDEO_HOSTS = {
    "youtube.com": "youtube",
    "youtube-nocookie.com": "youtube",
    "youtu.be": "youtube",
    "instagram.com": "reel",
    "instagr.am": "reel",
    "tiktok.com": "tiktok",
}
_HOP_REFUSALS = {
    "scheme": (400, "Links must be https"),
    "url": (400, "Not a valid link"),
    "dns": (502, "Site not found"),
    "private": (400, "Host is not public"),
}


class RecipeImportError(Exception):
    """A link that cannot be imported. `status` and `detail` map onto NoteError."""

    def __init__(self, detail: str, status: int = 400):
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class FetchedPage:
    blocks: list[dict]  # every top-level JSON-LD object on the page
    text: str  # the visible text, for the AI step when there is no recipe data
    url: str  # the final URL, after redirects
    title: str | None


def classify_url(url: str) -> str:
    """The kind of link: "youtube", "reel" (Instagram), "tiktok" or "website"."""
    raw = (url or "").strip()
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (httpx.URL(raw).host or "").lower().rstrip(".")
    except (httpx.InvalidURL, UnicodeError):  # UnicodeError: a host that is not IDNA
        return "website"
    for domain, kind in _VIDEO_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return kind
    return "website"


# -- fetch --------------------------------------------------------------------


_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head", "iframe"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_BLOCK_TAGS = {
    "p", "br", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
    "ul", "ol", "dt", "dd", "table", "section", "article", "header", "footer", "blockquote",
}
# Real pages nest a few dozen deep; the cap keeps a hostile page linear.
_MAX_DEPTH = 256


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ld_json: list[str] = []
        self.text: list[str] = []
        self.title: str | None = None
        self._stack: list[str] = []
        self._skipping = 0  # open _SKIP_TAGS on the stack
        self._in_ld = False
        self._in_title = False

    def _close_from(self, i: int) -> None:
        self._skipping -= sum(t in _SKIP_TAGS for t in self._stack[i:])
        del self._stack[i:]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Void elements never close; on the stack they would hide the end of
        # <head>, and with it every word of the page.
        if tag == "body" and "head" in self._stack:  # an unclosed <head>
            self._close_from(self._stack.index("head"))
        if tag not in _VOID_TAGS and len(self._stack) < _MAX_DEPTH:
            self._stack.append(tag)
            self._skipping += tag in _SKIP_TAGS
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower().strip() == "application/ld+json":
            self._in_ld = True
            self.ld_json.append("")
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        # Close back to the matching tag, so an unclosed <p> or <li> does not stay open.
        if tag in self._stack:
            self._close_from(len(self._stack) - 1 - self._stack[::-1].index(tag))
        if tag in _BLOCK_TAGS:
            self.text.append("\n")
        if tag == "script":
            self._in_ld = False
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self.ld_json[-1] += data
            return
        if self._in_title and not self.title:
            self.title = data.strip() or None
        if self._skipping:
            return
        self.text.append(data)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        },
    )


async def _require_public_https(url: httpx.URL) -> None:
    refusal = await media_lookup.public_https_refusal(url)
    if refusal:
        status, detail = _HOP_REFUSALS[refusal]
        raise RecipeImportError(detail, status)


async def _read_page(resp) -> tuple[bytes, str]:
    """The body of a final response, capped; returns (bytes, content-type header)."""
    if not 200 <= resp.status_code < 300:
        raise RecipeImportError(f"Could not fetch the page (HTTP {resp.status_code})", 502)
    header = resp.headers.get("content-type") or ""
    ctype = header.split(";")[0].strip().lower()
    if ctype and ctype not in PAGE_TYPES:
        raise RecipeImportError("Not a web page", 415)
    if media_lookup.stacked_encoding(resp):
        raise RecipeImportError("Could not fetch the page", 502)
    declared = resp.headers.get("content-length") or ""
    if declared.isdigit() and int(declared) > PAGE_MAX_BYTES:
        raise RecipeImportError("The page is too large", 413)
    size = 0
    chunks = []
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > PAGE_MAX_BYTES:
            raise RecipeImportError("The page is too large", 413)
        chunks.append(chunk)
    body = b"".join(chunks)
    # No content type at all: accept markup, refuse a PDF or an image.
    if not ctype and body.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] != b"<":
        raise RecipeImportError("Not a web page", 415)
    return body, header


async def fetch(url: str) -> FetchedPage:
    """Download one web page and collect its JSON-LD, visible text and title.

    Redirects are followed by hand so every hop is checked; the whole fetch,
    parsing included, shares one deadline and the size cap. Raises
    RecipeImportError.
    """
    try:
        target = httpx.URL((url or "").strip())
    except httpx.InvalidURL:
        raise RecipeImportError("Not a valid link")
    try:
        async with asyncio.timeout(PAGE_TIMEOUT), _client() as client:
            for _hop in range(PAGE_MAX_REDIRECTS + 1):
                await _require_public_https(target)
                async with client.stream("GET", str(target), follow_redirects=False) as resp:
                    location = resp.headers.get("location")
                    if 300 <= resp.status_code < 400 and location:
                        target = target.join(location)
                        continue
                    body, header = await _read_page(resp)
                return await asyncio.to_thread(_read_html, body, header, str(target))
            raise RecipeImportError("Too many redirects", 502)
    except TimeoutError:
        raise RecipeImportError("The page took too long", 504)
    # UnicodeError: httpx reads a redirect's host as IDNA before it answers,
    # and a Location such as https://xn--localhost/ is not.
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
        raise RecipeImportError("Could not fetch the page", 502)


def _decode(body: bytes, header: str) -> str:
    m = re.search(r"charset=[\"']?([\w.:-]+)", header, re.I)
    if m:
        charset = m.group(1)
    else:
        mb = re.search(rb"charset=[\"']?([\w.:-]+)", body[:4096], re.I)
        charset = mb.group(1).decode("ascii") if mb else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _load_ld(raw: str) -> list[dict]:
    raw = raw.strip()
    # Some CMSs wrap the block in a CDATA section.
    raw = re.sub(r"^(?://\s*)?<!\[CDATA\[|(?://\s*)?\]\]>$", "", raw).strip()
    try:
        data = json.loads(raw, strict=False)  # strict=False: raw newlines inside strings
    except (ValueError, RecursionError):
        return []
    items = data if isinstance(data, list) else [data]
    return [x for x in items if isinstance(x, dict)]


def _read_html(body: bytes, header: str, url: str) -> FetchedPage:
    parser = _Collector()
    parser.feed(_decode(body, header))
    parser.close()
    blocks: list[dict] = []
    for raw in parser.ld_json:
        blocks.extend(_load_ld(raw))
    text = re.sub(r"[ \t]+", " ", "".join(parser.text))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return FetchedPage(blocks=blocks, text=text, url=url, title=parser.title)


# -- schema.org -> Draft ----------------------------------------------------------


def _types(obj: dict) -> list[str]:
    """@type values without a vocabulary prefix: "schema:Recipe" and
    "http://schema.org/Recipe" are both "recipe"."""
    t = obj.get("@type")
    values = t if isinstance(t, list) else [t]
    return [re.split(r"[/:]", x)[-1].lower() for x in values if isinstance(x, str)]


def _is_recipe(obj: object) -> bool:
    return isinstance(obj, dict) and "recipe" in _types(obj)


def find_recipe(blocks: list[dict]) -> dict | None:
    """The first schema.org Recipe in the JSON-LD blocks, looking inside
    @graph, mainEntity, itemListElement and hasPart. None when there is none."""
    queue: deque[object] = deque(blocks)  # a list's pop(0) was quadratic in a huge @graph
    while queue:
        obj = queue.popleft()
        if _is_recipe(obj):
            return obj  # type: ignore[return-value]
        if isinstance(obj, dict):
            queue.extend(v for k, v in obj.items() if k in ("@graph", "mainEntity", "itemListElement", "hasPart"))
        elif isinstance(obj, list):
            queue.extend(obj)
    return None


_ISO_DURATION_RE = re.compile(
    r"P(?:(\d+(?:\.\d+)?)D)?T?(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", re.I
)
# The full ISO form some sites emit, years and months included: "P0Y0M0DT0H35M0S".
_ISO_FULL_RE = re.compile(
    r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?",
    re.I,
)


def iso_duration_minutes(value: object) -> int | None:
    """Minutes in an ISO 8601 duration ("PT1H30M" is 90); a few seconds round up to 1."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    years = months = weeks = 0.0
    m = _ISO_DURATION_RE.fullmatch(s)
    if m:
        d, h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
    else:
        m = _ISO_FULL_RE.fullmatch(s)
        if not m:
            # Not ISO at all: "1 hour 30 minutes", "25 mins".
            minutes = _duration_minutes(s)
            return minutes or None
        years, months, weeks, d, h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
    minutes = (years * 365 + months * 30 + weeks * 7 + d) * 1440 + h * 60 + mi
    if sec and not (h or mi):
        minutes += 1
    if not math.isfinite(minutes) or minutes > MAX_DURATION_MINUTES:
        return None
    return round(minutes)


def _first_int(value: object) -> int | None:
    values = value if isinstance(value, list) else [value]
    for v in values:
        if isinstance(v, dict):  # QuantitativeValue
            v = v.get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if math.isfinite(v):
                return int(min(v, 10**6))
        if isinstance(v, str):
            m = re.search(r"\d+", v)
            if m:
                return int(m.group()) if len(m.group()) <= 6 else 10**6
    return None


# [^<>]: a tag that never closes stops at the next "<" instead of rescanning
# to the end of the text from every one of them.
_BREAK_TAG_RE = re.compile(r"<\s*(?:br|/p|/li|/div)\b[^<>]*>", re.I)


def _strip_html(s: str, *, lines: bool = False) -> str:
    """Tags out, entities decoded. With `lines`, <br>, </p> and </li> become line breaks."""
    if lines:
        s = _BREAK_TAG_RE.sub("\n", s)
    s = html.unescape(re.sub(r"<[^<>]+>", " ", s)).replace("\xa0", " ")
    if lines:
        # Every space but the line break: a run of em spaces or form feeds
        # left in a step made the step-linking pattern quadratic.
        return re.sub(r"[^\S\n]+", " ", s).strip()
    return re.sub(r"\s+", " ", s).strip()


def _instructions(value: object) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        parts = re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", _strip_html(value, lines=True))
        out.extend(p.strip() for p in parts if p.strip())
    elif isinstance(value, list):
        for v in value:
            out.extend(_instructions(v))
    elif isinstance(value, dict):
        if "itemListElement" in value:  # HowToSection, or an ItemList of steps
            out.extend(_instructions(value.get("itemListElement")))
        else:
            text = value.get("text") or value.get("name")
            if isinstance(text, str) and _strip_html(text):
                out.append(_strip_html(text))
    return out


def _image(value: object, page_url: str) -> str | None:
    """The first image as an absolute https URL; anything else is dropped."""
    if isinstance(value, str):
        try:
            candidate = urljoin(page_url, value.strip())
        except ValueError:  # a malformed IPv6 host
            return None
        return candidate if candidate.startswith("https://") and " " not in candidate else None
    if isinstance(value, list):
        for v in value:
            found = _image(v, page_url)
            if found:
                return found
        return None
    if isinstance(value, dict):  # ImageObject; an {"@id": ...} reference has no URL to use
        for key in ("url", "contentUrl"):
            found = _image(value.get(key), page_url)
            if found:
                return found
    return None


def _tags(data: dict) -> list[str]:
    tags: list[str] = []
    for key in ("recipeCategory", "recipeCuisine", "keywords"):
        v = data.get(key)
        items = v if isinstance(v, list) else re.split(r",", v) if isinstance(v, str) else []
        tags.extend(t.strip().lower() for t in items if isinstance(t, str) and 0 < len(t.strip()) <= 24)
    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return seen[:8]


def draft_from_schema(data: dict, url: str) -> Draft:
    """A Draft from one schema.org Recipe object. Never raises on odd data:
    what cannot be read is left out and listed under `missing`."""
    missing: list[str] = []
    budget = MAX_SCHEMA_TEXT
    cut = False

    def within(texts):
        """The texts as far as MAX_SCHEMA_TEXT reaches, ingredients and steps
        together; the last one may be cut short."""
        nonlocal budget, cut
        for text in texts:
            if len(text) > budget:
                cut = True
                text = text[:budget]
            budget -= len(text)
            if text:
                yield text
            if cut:
                return

    name = data.get("name")
    title = _strip_html(name)[:200].strip() if isinstance(name, str) else ""
    if not title:
        title = "Untitled recipe"
        missing.append("No title in the page data.")

    servings = _first_int(data.get("recipeYield"))
    if servings is None or servings <= 0:
        servings = 4
        missing.append("Servings not found; set to 4.")

    prep = iso_duration_minutes(data.get("prepTime"))
    cook = iso_duration_minutes(data.get("cookTime"))
    total = iso_duration_minutes(data.get("totalTime"))
    if cook is None and total is not None:
        cook = max(0, total - (prep or 0))
        if prep is None:
            missing.append("Total time stored as cook time.")

    ingredients: list[Ingredient] = []
    raw_ings = data.get("recipeIngredient") or data.get("ingredients") or []
    if isinstance(raw_ings, str):
        raw_ings = raw_ings.splitlines()
    lines = raw_ings if isinstance(raw_ings, list) else []
    for line in within(_strip_html(line) for line in lines if isinstance(line, str)):
        ing = parse_ingredient_line(line)
        if ing:
            ingredients.append(ing)
    if not ingredients:
        missing.append("No ingredients in the page data.")
    no_amount = [i.item for i in ingredients if i.amount is None and i.scaling != "to_taste"]
    if no_amount:
        missing.append("No amount for: " + ", ".join(no_amount) + ".")

    steps = [
        Step(text=t, timer_seconds=_timer_seconds(t))
        for t in within(_instructions(data.get("recipeInstructions")))
    ]
    if not steps:
        missing.append("No steps in the page data.")
    if cut:
        missing.append(f"The recipe on the page was longer than {MAX_SCHEMA_TEXT:,} characters and was cut.")

    dedupe_ids(ingredients)
    link_references(steps, ingredients)

    recipe = RecipeIn(
        title=title,
        base_servings=min(servings, 100),
        prep_minutes=prep,
        cook_minutes=cook,
        ingredients=ingredients,
        steps=steps,
        notes="",
        tags=_tags(data),
        source=Source(type="website", url=url, thumbnail=_image(data.get("image"), url)),
    )
    return Draft(recipe=recipe, missing=missing, method="schema_org")
