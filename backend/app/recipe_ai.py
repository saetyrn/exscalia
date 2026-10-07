"""The AI step for Recipes.

`extract` reads typed text, a web page's text, photos and PDFs and returns a
recipe draft to review; `draw` turns a recipe into a small coloured ASCII
drawing of the dish. Neither writes anything: the import route shows the
Draft, and the store keeps the Drawing.

Both ride the app's provider layer (Anthropic, OpenAI or Google, as set in
Settings) on the Recipes model (`effective_recipes_model`, which falls back to
the chat model) at the Recipes effort (`recipes_reasoning`: the owner's level
for both, else medium for imports and low for drawings).
The provider layer has no structured-output mode, so each call asks for JSON
that validates against the Pydantic schema, checks the reply, and on a
mismatch asks once more with the validation error before giving up.

Metering: every call is recorded under the caller "recipes". A drawing is
background work and passes the budget gate first; an import is the user
waiting on an answer and, like chat, is never paused by the budget.
"""
import asyncio
import base64
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from .overseer import background, vision
from .overseer.provider import (
    build_provider,
    document_block,
    fence,
    fence_nonce,
    image_block,
    max_output_tokens,
)
from .overseer.runtime_config import (
    effective_ai,
    effective_recipes_model,
    load_ai_config,
    recipes_reasoning,
)
from .recipe_models import Draft, Drawing, Ingredient, RecipeIn, Scaling, Source, Step, Unit
from .vault.notes import NoteError

log = logging.getLogger(__name__)

# The usage ledger's name for everything this module spends.
CALLER = "recipes"

MAX_SOURCE_CHARS = 200_000

# Uploads, as the import route receives them. A phone photo is 3 to 12 MB
# before it is downscaled; PDFs keep the chat's ceilings (8 MB, 50 pages).
MAX_FILES = 6
MAX_IMAGE_BYTES = 20 * 1024 * 1024
# Everything in one request after downscaling; the providers stop at 32 MB.
MAX_TOTAL_BYTES = 24 * 1024 * 1024

PDF_TYPE = "application/pdf"
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")

NO_PROVIDER = "No AI provider is configured."
# What `explain` says about a failure it does not recognise.
UNEXPLAINED = "The AI request failed."


class RecipeAIError(Exception):
    """A failure to show the user as it is: one short sentence. `status` is
    the HTTP answer for it: 400 or 413 for what was sent, 503 when no
    provider is configured, 502 when the AI itself failed."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SourceFile:
    """One uploaded photo or PDF of a recipe: the raw bytes off the multipart
    form, the content type the browser declared, and the file name for error
    messages. The real type is read from the bytes."""

    data: bytes
    media_type: str = ""
    name: str = ""


def _config(state) -> dict:
    return getattr(state, "ai_config", None) or load_ai_config(state.settings.data_dir)


def available(state) -> bool:
    """Whether a provider is configured, so the AI step can run."""
    _provider_id, key, _model = effective_ai(state.settings, _config(state))
    return bool(key)


def explain(exc: Exception) -> str:
    """One plain sentence for a provider failure, whichever SDK raised it."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        code = getattr(exc, "code", None)  # google-genai's APIError
        status = code if isinstance(code, int) else None
    kind = type(exc).__name__
    text = str(getattr(exc, "message", "") or exc).lower()
    if any(
        marker in text
        for marker in ("credit balance", "insufficient_quota", "exceeded your current quota", "billing")
    ):
        return "The AI account is out of credit."
    if (
        status in (401, 403)
        or kind in ("AuthenticationError", "PermissionDeniedError")
        or "incorrect api key" in text
        or ("api key" in text and ("invalid" in text or "not valid" in text))
    ):
        return "The AI key was rejected."
    if status == 429 or kind == "RateLimitError" or "rate limit" in text or "resource_exhausted" in text:
        return "The AI is rate limited. Try again in a minute."
    if status in (500, 502, 503, 504, 529) or "overloaded" in text or "unavailable" in text:
        return "The AI service is busy. Try again shortly."
    if kind in ("APIConnectionError", "APITimeoutError") or isinstance(
        exc, (TimeoutError, ConnectionError)
    ):
        return "Could not reach the AI service."
    return f"The AI request failed ({status})." if status else UNEXPLAINED


def _provider(state, config: dict, *, reasoning: str, max_output: int | None = None):
    """A provider on the Recipes model at `reasoning`. Never the chat
    provider: the effort decides between ten seconds and ten minutes here."""
    provider_id, key, _model = effective_ai(state.settings, config)
    if not key:
        raise RecipeAIError(NO_PROVIDER, 503)
    model = effective_recipes_model(state.settings, config)
    try:
        built = build_provider(
            state.settings,
            provider_id=provider_id,
            api_key=key,
            model=model,
            reasoning=reasoning,
            max_output=max_output,
        )
    except Exception:
        log.warning("Recipes model %s unavailable", model, exc_info=True)
        built = None
    if built is None:
        raise RecipeAIError(NO_PROVIDER, 503)
    return built


# ---------------------------------------------------------------- JSON replies

T = TypeVar("T", bound=BaseModel)

JSON_RULES = (
    "Answer with one JSON object and nothing else: no prose before or after it "
    "and no code fences. It must validate against this JSON Schema:\n"
)

RETRY = "That answer did not match the schema: {problem}\nAnswer again with only the corrected JSON object."


async def _discard(_text: str) -> None:
    """Streaming keeps a long think alive past the provider's read timeout;
    the reply is read whole at the end."""


def _parse(text: str, schema: type[T]) -> T:
    """The reply as `schema`. Raises ValueError (ValidationError is one)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("the answer held no JSON object")
    value, _end = json.JSONDecoder().raw_decode(text, start)
    return schema.model_validate(value)


def _problem(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        lines = [
            f"{'.'.join(str(part) for part in error['loc']) or 'answer'}: {error['msg']}"
            for error in exc.errors()[:8]
        ]
        return "\n".join(lines)
    return str(exc)[:500]


def _record(state, reply) -> None:
    ledger = getattr(state, "usage", None)
    if ledger is not None:
        ledger.record(CALLER, getattr(reply, "usage", None))


async def _ask(
    state, provider, system: str, content, schema: type[T], *, cut_off: str, declined: str
) -> T:
    """One structured answer: ask, validate, and on a mismatch ask once more
    with the validation error in hand."""
    prompt = system + "\n\n" + JSON_RULES + json.dumps(schema.model_json_schema())
    messages: list[dict] = [{"role": "user", "content": content}]
    for attempt in range(2):
        try:
            reply = await provider.chat(prompt, messages, [], _discard)
        except Exception as exc:
            sentence = explain(exc)
            # A rate limit, a bad key or an outage is the provider's answer:
            # one line. A failure nothing recognises may be a bug here and
            # keeps its traceback.
            log.warning(
                "recipe AI call failed: %s: %s",
                type(exc).__name__,
                str(exc)[:300],
                exc_info=sentence == UNEXPLAINED,
            )
            raise RecipeAIError(sentence) from exc
        # Metered first, because the tokens were spent.
        _record(state, reply)
        if getattr(reply, "refused", False):
            raise RecipeAIError(declined)
        if getattr(reply, "hit_output_limit", False):
            raise RecipeAIError(cut_off)
        text = getattr(reply, "text", "") or ""
        try:
            return _parse(text, schema)
        except ValueError as exc:
            problem = _problem(exc)
            log.info("recipe AI answer did not match %s: %s", schema.__name__, problem)
        if attempt == 0:
            # The reply as the provider gave it (Claude's thinking included),
            # unless it carries no text to correct.
            said = list(getattr(reply, "raw_content", None) or [])
            if not any(b.get("type") == "text" and b.get("text") for b in said):
                said = [{"type": "text", "text": text or "(no answer)"}]
            messages = [
                *messages,
                {"role": "assistant", "content": said},
                {"role": "user", "content": RETRY.format(problem=problem)},
            ]
    raise RecipeAIError("The AI answer did not match the recipe format.")


# ---------------------------------------------------------------- import


# The structure the model must return. Every field is required so the schema is strict.
class AIIngredient(BaseModel):
    id: str
    amount: float | None
    unit: Unit
    item: str
    prep: str
    scaling: Scaling
    estimated: bool
    group: str | None


class AIStep(BaseModel):
    text: str
    timer_seconds: int | None


class AIRecipe(BaseModel):
    title: str
    base_servings: int
    prep_minutes: int | None
    cook_minutes: int | None
    ingredients: list[AIIngredient]
    steps: list[AIStep]
    notes: str
    tags: list[str]
    source_kind: Literal["text", "photo_of_text", "photo_of_dish", "webpage", "unknown"]
    missing: list[str]


SYSTEM = """You convert recipes from any source into one fixed structured format for a personal recipe app.

Rules:
- Return only the structure, nothing else.
- The title in normal title case ("Shakshuka", not "SHAKSHUKA"), even when the source is in capitals.
- Amounts are plain numbers the app can do math on. Write fractions as decimals (1/2 -> 0.5). Units come only from the fixed list; use "" for counted items such as eggs or onions. Choose the unit the source used.
- Never invent an amount silently. When the source gives no amount or says "a splash", "some", "a handful", put in a sensible guess and set estimated to true. Salt, pepper and spices with no amount may have amount null and scaling "to_taste".
- scaling: "whole" for items that only come in whole numbers (eggs, tortillas, chicken thighs); "to_taste" for salt, pepper, spices, hot sauce; "linear" for everything else.
- id: a short lowercase slug of the item (butter, soy-sauce), unique within the recipe.
- prep: how the ingredient is prepared (melted, diced, room temperature). group: an optional sub-heading such as "Sauce", else null.
- Steps: one action each, short. Where a step mentions an ingredient together with its amount, write {id} in place of the typed amount and name, for example "Toss the noodles with {butter}". Set timer_seconds only when the step has a duration; use the lower number of a range.
- base_servings: the servings the amounts are written for. If the source does not say, use 4 and add "Servings not stated; set to 4." to missing.
- prep_minutes and cook_minutes: null when not stated.
- tags: 2 to 5 short lowercase tags: course, cuisine, main ingredient, and "quick" when the total time is under 30 minutes.
- notes: "" unless the source has a tip worth keeping; then one or two short lines.
- missing: everything the reader must check. If the source says the recipe is elsewhere ("link in bio", "recipe in comments"), say exactly that. Mention unreadable text, amounts you guessed, and anything the source left out.
- A photo of a finished dish with no written recipe: propose a plausible recipe, mark every amount estimated, set source_kind "photo_of_dish", and say in missing that the whole recipe is a proposal.
- source_kind: what the input was."""


def _source_type(kind: str) -> str:
    return {
        "text": "text",
        "photo_of_text": "photo",
        "photo_of_dish": "dish_photo",
        "webpage": "website",
    }.get(kind, "text")


_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Ingredient ids may be 64 characters; this leaves room for a "-12".
MAX_ID_CHARS = 48


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:MAX_ID_CHARS].strip("-")


def _ingredient_id(raw: str, item: str) -> str:
    if _SLUG.fullmatch(raw) and len(raw) <= MAX_ID_CHARS:
        return raw
    return _slug(raw) or _slug(item) or "item"


def _dedupe_ids(ingredients: list[Ingredient]) -> None:
    """A repeated id gets -2, -3, ...; steps keep naming the first."""
    taken: set[str] = set()
    for ing in ingredients:
        base, n = ing.id, 1
        while ing.id in taken:
            n += 1
            ing.id = f"{base}-{n}"
        taken.add(ing.id)


def _relink(text: str, renamed: dict[str, str]) -> str:
    """Follow ids that were reslugged: {Soy Sauce} becomes {soy-sauce}."""
    if not renamed:
        return text
    return re.sub(r"\{([^{}]+)\}", lambda m: "{" + renamed.get(m.group(1), m.group(1)) + "}", text)


def _to_draft(parsed: AIRecipe, url: str | None, thumbnail: str | None, from_image: bool) -> Draft:
    renamed: dict[str, str] = {}
    ingredients = []
    for i in parsed.ingredients:
        ident = _ingredient_id(i.id, i.item)
        if ident != i.id:
            renamed.setdefault(i.id, ident)
        ingredients.append(
            Ingredient(
                id=ident,
                amount=i.amount,
                unit=i.unit,
                item=i.item or "unknown",
                prep=i.prep,
                scaling=i.scaling,
                estimated=i.estimated,
                group=i.group or None,
            )
        )
    _dedupe_ids(ingredients)
    steps = [
        Step(
            text=_relink(s.text, renamed),
            timer_seconds=s.timer_seconds if s.timer_seconds and s.timer_seconds > 0 else None,
        )
        for s in parsed.steps
        if s.text.strip()
    ]
    kind = parsed.source_kind
    if from_image and kind in ("text", "webpage", "unknown"):
        kind = "photo_of_text"
    recipe = RecipeIn(
        title=(parsed.title.strip() or "Untitled recipe")[:200],
        base_servings=max(1, min(parsed.base_servings or 4, 100)),
        prep_minutes=parsed.prep_minutes if parsed.prep_minutes is not None and parsed.prep_minutes >= 0 else None,
        cook_minutes=parsed.cook_minutes if parsed.cook_minutes is not None and parsed.cook_minutes >= 0 else None,
        ingredients=ingredients,
        steps=steps,
        notes=parsed.notes,
        tags=parsed.tags[:8],
        source=Source(type=_source_type(kind), url=url, thumbnail=thumbnail),  # type: ignore[arg-type]
    )
    return Draft(recipe=recipe, missing=list(parsed.missing), method="ai")


def sniff(data: bytes) -> str | None:
    """An upload's real type from its first bytes, or None if it is neither
    a photo the providers read nor a PDF."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # The header may sit anywhere in a PDF's first kilobyte.
    if b"%PDF" in data[:1024]:
        return PDF_TYPE
    return None


def _encode_files(files: Sequence[SourceFile]) -> tuple[list[dict], int, int]:
    """Provider blocks for the uploads, with the count of photos and PDFs.
    Blocking (Pillow, pypdf): run it in a thread."""
    blocks: list[dict] = []
    photos = pdfs = total = 0
    for file in files:
        if not file.data:
            continue
        label = f"“{file.name}”" if file.name else "The file"
        actual = sniff(file.data)
        declared = (file.media_type or "").split(";")[0].strip().lower()
        if declared == PDF_TYPE and actual != PDF_TYPE:
            raise RecipeAIError(f"{label} is not a PDF.", 400)
        if actual is None:
            raise RecipeAIError(f"{label} is not a photo or a PDF.", 400)
        if actual == PDF_TYPE:
            if len(file.data) > vision.MAX_DOCUMENT_BYTES:
                raise RecipeAIError(
                    f"{label} is larger than {vision.MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.", 413
                )
            if vision.page_count(file.data) > vision.MAX_DOCUMENT_PAGES:
                raise RecipeAIError(f"{label} has more than {vision.MAX_DOCUMENT_PAGES} pages.", 400)
            block = document_block(PDF_TYPE, base64.b64encode(file.data).decode("ascii"))
            # Anthropic's document block names itself with "title".
            block.pop("name", None)
            if file.name:
                block["title"] = file.name[:200]
            size = len(file.data)
            pdfs += 1
        else:
            if len(file.data) > MAX_IMAGE_BYTES:
                raise RecipeAIError(f"{label} is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB.", 413)
            try:
                encoded = vision.encode_image(file.data, actual, label=label)
            except NoteError as exc:
                raise RecipeAIError(exc.detail.rstrip(".") + ".", exc.status) from exc
            block = image_block(encoded["media_type"], encoded["data"])
            size = len(encoded["data"]) * 3 // 4
            photos += 1
        total += size
        if total > MAX_TOTAL_BYTES:
            raise RecipeAIError(
                f"The files are larger than {MAX_TOTAL_BYTES // (1024 * 1024)} MB together. Send fewer at once.",
                413,
            )
        blocks.append(block)
    return blocks, photos, pdfs


def _default_hint(*, url: str | None, photos: int, pdfs: int) -> str:
    count = photos + pdfs
    if count:
        what = "PDF" if not photos else "photo" if not pdfs else "file"
        what = f"these {what}s" if count > 1 else f"this {what}"
        return (
            f"Read the recipe in {what} (a cookbook page, recipe card, printout, screenshot, or a finished dish) "
            "and convert it. Several files are pages of the same recipe."
        )
    if url:
        return "This is the text of a web page. Find the recipe in it and convert it."
    return "Convert this typed or pasted recipe text."


async def extract(
    state,
    *,
    text: str | None = None,
    url: str | None = None,
    files: Sequence[SourceFile] = (),
    hint: str | None = None,
) -> Draft:
    """A recipe draft from typed text, a web page's text (`url` is where it
    came from), photos and PDFs. Guessed amounts come back `estimated`, and
    `missing` lists what the source did not give. Raises RecipeAIError."""
    config = _config(state)
    provider = _provider(state, config, reasoning=recipes_reasoning(config, "import"))
    if len(files) > MAX_FILES:
        raise RecipeAIError(f"Send at most {MAX_FILES} files at once.", 400)
    blocks, photos, pdfs = await asyncio.to_thread(_encode_files, files) if files else ([], 0, 0)
    text = text or ""
    if not blocks and not text.strip():
        raise RecipeAIError("The files were empty." if files else "There is nothing to read.", 400)

    prompt = hint or _default_hint(url=url, photos=photos, pdfs=pdfs)
    if url:
        prompt += f"\nSource URL: {url}"
    truncated = len(text) > MAX_SOURCE_CHARS
    if truncated:
        text = text[:MAX_SOURCE_CHARS]
    if text.strip():
        # Quoted data: a page cannot close the fence and carry on as prompt.
        prompt += "\n\n" + fence(text, fence_nonce(text), "page" if url else "text")

    parsed = await _ask(
        state,
        provider,
        SYSTEM,
        [*blocks, {"type": "text", "text": prompt}],
        AIRecipe,
        cut_off="The AI answer was cut off. Try a shorter source.",
        declined="The AI declined to read this source.",
    )
    draft = _to_draft(parsed, url, None, bool(blocks))
    if truncated:
        draft.missing.append(
            f"The source text was longer than {MAX_SOURCE_CHARS:,} characters and was cut."
        )
    return draft


# ---------------------------------------------------------------- ASCII art

ART_COLS = 40
ART_ROWS = 18
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class AIPaletteColor(BaseModel):
    key: str = Field(description="One lowercase letter a-z used in the colour grid.")
    color: str = Field(description="Hex colour, #rrggbb.")
    name: str = Field(description="What it paints, e.g. 'noodle gold', 'scallion green', 'bowl glaze'.")


class AIArt(BaseModel):
    palette: list[AIPaletteColor]
    rows: list[str]
    colors: list[str]


ART_SYSTEM = f"""You are an ASCII artist. You draw small coloured illustrations of food for a recipe app
with a pure black background. The drawing is shown tiny, about 200 pixels wide, so the one thing
that matters most is that it reads instantly: someone glancing at it should know what dish it is.
Choose readability over detail every time.

CANVAS
- Exactly {ART_COLS} columns by {ART_ROWS} rows of monospaced characters (pad with spaces).
- A character cell is about twice as tall as it is wide: draw a round plate about twice as wide as
  it is tall.
- Centre the dish and keep a margin of empty space around it. Empty space is good: it makes the
  shape stand out.

WHAT MAKES IT READABLE
- One clear silhouette. Start from the overall outline of the dish in its vessel (bowl, plate, pan,
  pot, glass, board) and make that outline clean, smooth and continuous, so the shape alone says
  "a bowl of noodles" or "a stack of pancakes".
- A few big shapes, not many small ones. Show the two or three ingredients that identify the dish,
  drawn large enough to recognise; leave out minor ones. A recognisable fried egg beats ten specks.
- Calm interiors. Inside a shape, repeat one or two characters in a simple pattern (~~~ for noodles,
  === for a pancake, ooo for rice) rather than mixing many characters. Avoid noise: no random
  scattering of symbols, no dense clusters of # % @ that turn into a blur at small size.
- Shading only where it helps the form read: a darker underside or shadow, one highlight on a
  glossy surface. Light to dense, if needed: . : - = + * #
- Steam, if the dish is hot: two or three short wisps of ( ) above it, no more.
- Never letters, words, numbers or labels.

COLOUR
- A palette of 3 to 6 colours. Each has a one-letter key (a-z), a hex colour and a short name.
- Colour is for telling parts apart: give each main ingredient and the vessel its own clearly
  different colour, with strong contrast between neighbouring parts. Take hues from the real food
  (golden noodle yellow, egg-yolk orange, scallion green, chilli red, toast brown) and a cool grey or
  glaze colour for the vessel. Bright enough to read on black (lightness about 60-80%), natural, not
  neon. One warm off-white for steam, rice, cream and egg white.
- Keep each colour in solid areas; do not alternate colours character by character.
- `colors` is a grid the same size as `rows`: each character is the palette key for the character at
  the same position in `rows`, or "." for a space. Every non-space character needs a key.

OUTPUT
- palette, then rows (exactly {ART_ROWS} strings of exactly {ART_COLS} characters), then colors
  (same shape). Plain printable ASCII only (space through tilde): no tabs, no Unicode, no fences.
- Before writing, plan: the silhouette, the two or three parts that identify the dish, and their
  colours. Then check the result would still read if it were half the size."""


def clean_art(
    raw_rows: list[str] | str,
    raw_colors: list[str] | str | None = None,
    palette: dict[str, str] | None = None,
) -> Drawing:
    """Make whatever the model sent a tidy, fixed-size drawing with a colour grid that lines up."""

    def lines(v: list[str] | str | None) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = v.replace("\r", "").split("\n")
        return [line for line in v if not line.strip().startswith("```")]

    rows = [
        "".join(ch if 32 <= ord(ch) <= 126 else " " for ch in r.replace("\t", "    "))
        for r in lines(raw_rows)
    ]
    cols = lines(raw_colors)
    cols += [""] * (len(rows) - len(cols))
    pairs = list(zip(rows, cols))
    # Drop blank rows at the edges, then shift left by the shared indent.
    while pairs and not pairs[0][0].strip():
        pairs.pop(0)
    while pairs and not pairs[-1][0].strip():
        pairs.pop()
    indent = min((len(r) - len(r.lstrip(" ")) for r, _ in pairs if r.strip()), default=0)
    pairs = [(r[indent:], c[indent:]) for r, c in pairs][:ART_ROWS]

    pal = {
        k: v
        for k, v in (palette or {}).items()
        if len(k) == 1 and k.isalpha() and k.islower() and HEX_RE.match(v)
    }
    out_rows, out_cols = [], []
    for r, c in pairs:
        r = r[:ART_COLS].rstrip()
        keys = []
        for i, ch in enumerate(r):
            k = c[i] if i < len(c) else "."
            keys.append(k if ch != " " and k in pal else ".")
        out_rows.append(r)
        out_cols.append("".join(keys))
    return Drawing(
        art="\n".join(out_rows), art_colors="\n".join(out_cols) if pal else "", art_palette=pal
    )


# A drawing above low effort thinks through most of its output before the
# first row, so it gets the model's whole ceiling, as the recipe app did:
# 128K on Claude (Haiku 4.5 stops at 64K), 64K on OpenAI. Gemini keeps the
# table's ceiling, which is above its largest thinking budget.
def art_output_ceiling(provider_id: str, model: str, level: str) -> int | None:
    if level == "low":
        return None
    if provider_id == "anthropic":
        ceiling = 64000 if model.startswith("claude-haiku") else 128000
    elif provider_id == "openai":
        ceiling = 64000
    else:
        return None
    return max(ceiling, max_output_tokens(provider_id, model))


def _art_prompt(recipe: RecipeIn) -> str:
    ingredients = "\n".join(
        f"- {i.item}" + (f" ({i.prep})" if i.prep else "") + (f" [{i.group}]" if i.group else "")
        for i in recipe.ingredients[:16]
    )
    steps = " / ".join(s.text for s in recipe.steps[-3:])
    return (
        f"Dish: {recipe.title}\n"
        f"Tags: {', '.join(recipe.tags) or 'none'}\n"
        f"Ingredients:\n{ingredients or '- unknown'}\n"
        f"How it is finished and served: {steps or 'not stated'}"
    )


async def draw(state, recipe: RecipeIn) -> Drawing:
    """A coloured ASCII drawing of the dish. Background work: refused while
    the monthly budget has paused unattended spending. Raises RecipeAIError."""
    config = _config(state)
    provider_id, key, _model = effective_ai(state.settings, config)
    if not key:
        raise RecipeAIError(NO_PROVIDER, 503)
    if not background.allowed(state, config):
        raise RecipeAIError("The monthly token budget has paused drawings.")
    level = recipes_reasoning(config, "drawing")
    ceiling = art_output_ceiling(provider_id, effective_recipes_model(state.settings, config), level)
    provider = _provider(state, config, reasoning=level, max_output=ceiling)
    out = await _ask(
        state,
        provider,
        ART_SYSTEM,
        _art_prompt(recipe),
        AIArt,
        cut_off="The drawing ran out of room before it finished.",
        declined="The AI declined to draw this.",
    )
    drawing = clean_art(out.rows, out.colors, {p.key.strip().lower(): p.color.strip() for p in out.palette})
    if not drawing.art.strip():
        raise RecipeAIError("The drawing came back empty.")
    return drawing
