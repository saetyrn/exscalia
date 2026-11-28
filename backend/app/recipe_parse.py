"""Local recipe parser: turns typed or pasted recipe text into a Draft without calling an AI.

It handles the common layout (title, "Serves N", an ingredient list, numbered steps) and
leaves anything it is unsure about for the review screen, listed under `missing`.
Ported from the standalone recipe app; it never raises on odd text, so the
import route can always fall back to it.
"""
from __future__ import annotations

import re

from .recipe_models import Draft, Ingredient, RecipeIn, Source, Step
from .recipe_units import default_scaling, unit_alias_pattern, unit_from_word

VULGAR = {"¼": 0.25, "½": 0.5, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875}
WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "dozen": 12, "half": 0.5,
}
VAGUE_WORDS = {"splash", "some", "handful", "dash", "drizzle", "glug", "knob", "bit", "few", "little", "sprinkle"}
MAX_SERVINGS = 100  # RecipeIn.base_servings
MAX_TITLE = 200  # RecipeIn.title
MAX_ID = 56  # Ingredient.id allows 64; room for a dedupe suffix
# Longer digit runs are not quantities; int() also refuses past 4,300 digits.
MAX_NUMBER = 12
# The most recipe text one parse reads. Linking matches every ingredient
# against every step, so the work grows with the square of the text; a typed
# recipe is a few thousand characters. The import route, OMNIA's import_recipe
# and the schema.org reader all cut at this.
MAX_TEXT = 50_000

_FRAC = "[¼½¾⅓⅔⅛⅜⅝⅞]"
_NUM = rf"(?:\d+\s+\d+/\d+|\d+/\d+|\d+[.,]\d+|\d+\s*{_FRAC}|{_FRAC}|\d+)"
_UNITS = unit_alias_pattern()

AMOUNT_RE = re.compile(rf"^\s*(?P<a>{_NUM})(?:\s*(?:-|–|—|to)\s*(?P<b>{_NUM}))?\s*(?P<rest>.*)$")
WORD_AMOUNT_RE = re.compile(r"^\s*(?P<w>a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve|half a|half|dozen)\s+(?P<rest>.+)$", re.I)
BULLET_RE = re.compile(r"^\s*(?:[-*•·▢☐✓]|\d+[.)]\s+(?=\D))\s*")
NUMBERED_STEP_RE = re.compile(r"^\s*(?:step\s*)?(\d+)\s*[.):\-]\s+(?P<text>.+)$", re.I)
HEAD_ING = re.compile(r"^\W*(ingredients?|what you(?:'ll| will)? need|shopping list)\W*$", re.I)
HEAD_STEPS = re.compile(r"^\W*(steps?|method|instructions?|directions?|preparation|how to make(?: it)?|procedure)\W*$", re.I)
HEAD_NOTES = re.compile(r"^\W*(notes?|tips?|chef'?s? notes?)\W*$", re.I)
GROUP_RE = re.compile(r"^(?:for the\s+)?(?P<name>[A-Za-z][A-Za-z '&-]{1,40})\s*:?\s*$", re.I)
SERVES_RE = re.compile(r"\b(?:serves|servings?|yields?|makes|portions?)\b\s*[:\-]?\s*(?:about\s+)?(\d+)", re.I)
FOR_PEOPLE_RE = re.compile(r"\bfor\s+(\d+)\s+(?:people|persons|servings?|portions?)\b", re.I)
N_SERVINGS_RE = re.compile(r"\b(\d+)\s+(?:servings?|portions?)\b", re.I)
_DUR = r"(?:\d+\s*(?:h(?:ou)?rs?|hours?|h|m(?:in(?:ute)?s?)?|minutes?)\b\s*(?:and\s*)?)+"
TIME_RE = re.compile(rf"\b(?P<kind>prep(?:aration)?|cook(?:ing)?|total|ready in|bake|baking|active|inactive)(?:\s*time)?\s*[:\-]?\s*(?P<dur>{_DUR})", re.I)
# (?<!\d): a match starts where a number starts. Without it a long run of
# digits costs quadratic time (20,000 digits took 11 seconds).
DUR_PART_RE = re.compile(r"(?<!\d)(\d+)\s*(h(?:ou)?rs?|hours?|h|m(?:in(?:ute)?s?)?|minutes?)\b", re.I)
TIMER_RE = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:(?:-|–|to)\s*\d+\s*)?(sec(?:ond)?s?|min(?:ute)?s?|h(?:ou)?rs?)\b", re.I)
TO_TASTE_RE = re.compile(r",?\s*\(?\bto taste\b\)?", re.I)
OPTIONAL_RE = re.compile(r",?\s*\(?\boptional\b\)?", re.I)
# [^()]: the innermost brackets. [^)] rescanned to the end of the line from
# every "(" that never closes, quadratic in a run of them.
PAREN_RE = re.compile(r"\s*\(([^()]*)\)")
MARKDOWN_RE = re.compile(r"^\s*#+\s*|\*\*|__|`")


def parse_number(s: str) -> float | None:
    s = s.strip().replace(",", ".")
    if len(s) > MAX_NUMBER:
        return None
    if s in VULGAR:
        return VULGAR[s]
    m = re.fullmatch(r"(\d+)\s*(" + _FRAC + ")", s)
    if m:
        return int(m.group(1)) + VULGAR[m.group(2)]
    m = re.fullmatch(r"(\d+)\s+(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3)) if int(m.group(3)) else None
    m = re.fullmatch(r"(\d+)/(\d+)", s)
    if m:
        return int(m.group(1)) / int(m.group(2)) if int(m.group(2)) else None
    try:
        return float(s)
    except ValueError:
        return None


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "-".join(s.split("-")[:3])[:MAX_ID].strip("-") or "item"


def parse_ingredient_line(line: str, group: str | None = None) -> Ingredient | None:
    """Parse one ingredient line. Returns None when the line is not an ingredient."""
    text = BULLET_RE.sub("", MARKDOWN_RE.sub("", line)).strip()
    if not text:
        return None

    # "Butter - 2 tbsp" / "Butter: 2 tbsp" layouts
    if not AMOUNT_RE.match(text) or not _looks_like_amount_start(text):
        for sep in (":", " - ", " – ", " — "):
            if sep in text:
                left, right = text.split(sep, 1)
                if _looks_like_amount_start(right.strip()) and left.strip():
                    parsed = parse_ingredient_line(f"{right.strip()} {left.strip()}", group)
                    if parsed:
                        return parsed

    amount: float | None = None
    estimated = False
    rest = text

    m = AMOUNT_RE.match(text)
    if m and _looks_like_amount_start(text):
        amount = parse_number(m.group("a"))
        if m.group("b"):
            estimated = True  # a range: we kept the first number, so it should be checked
        rest = m.group("rest")
    else:
        wm = WORD_AMOUNT_RE.match(text)
        if wm:
            amount = WORD_NUMBERS[wm.group("w").lower()]
            rest = wm.group("rest")

    unit = ""
    words = rest.split()
    if words:
        candidate = words[0].rstrip(".,") if len(words[0]) > 1 else words[0]
        u = unit_from_word(candidate) or unit_from_word(words[0])
        if u and len(words) > 1:
            unit = u
            rest = " ".join(words[1:])
        elif words[0].lower().rstrip(",") in VAGUE_WORDS and len(words) > 1:
            amount = None
            estimated = True
            rest = " ".join(words[1:])
    rest = re.sub(r"^(?:of\s+)", "", rest.strip(), flags=re.I)

    prep_parts: list[str] = []
    scaling: str | None = None
    if TO_TASTE_RE.search(rest):
        scaling = "to_taste"
        rest = TO_TASTE_RE.sub("", rest)
    if OPTIONAL_RE.search(rest):
        prep_parts.append("optional")
        rest = OPTIONAL_RE.sub("", rest)
    for pm in PAREN_RE.finditer(rest):
        prep_parts.append(pm.group(1).strip())
    rest = PAREN_RE.sub("", rest)
    if "," in rest:
        item, prep = rest.split(",", 1)
        prep_parts.insert(0, prep.strip(" ,"))
    else:
        item = rest
    item = re.sub(r"\s+", " ", item).strip(" .,;-")
    if not item or item.lower() in ("", "ingredients", "method"):
        return None
    if amount is None and unit == "" and scaling != "to_taste" and (len(item.split()) > 8 or rest.rstrip().endswith((".", "!"))):
        return None  # a sentence, not an ingredient

    if scaling is None:
        scaling = default_scaling(unit, item)
    return Ingredient(
        id=slug(item),
        amount=amount,
        unit=unit,  # type: ignore[arg-type]
        item=item,
        prep=", ".join(p for p in prep_parts if p),
        scaling=scaling,  # type: ignore[arg-type]
        estimated=estimated,
        group=group,
    )


def _looks_like_amount_start(text: str) -> bool:
    m = AMOUNT_RE.match(text)
    if not m:
        return False
    # "1. Preheat the oven" is a step, not an amount; require a unit or a noun right after.
    rest = m.group("rest")
    if rest.startswith((".", ")")):
        return False
    return True


def _duration_minutes(s: str) -> int:
    total = 0
    for n, unit in DUR_PART_RE.findall(s):
        if len(n) > MAX_NUMBER:
            continue
        total += int(n) * 60 if unit.lower().startswith("h") else int(n)
    return total


def _timer_seconds(text: str) -> int | None:
    m = TIMER_RE.search(text)
    if not m or len(m.group(1)) > MAX_NUMBER:
        return None
    n = float(m.group(1).replace(",", "."))
    u = m.group(2).lower()
    if u.startswith("sec"):
        return max(1, int(n))
    if u.startswith("min"):
        return max(1, int(n * 60))
    return max(1, int(n * 3600))


def dedupe_ids(ingredients: list[Ingredient]) -> None:
    seen: dict[str, int] = {}
    for ing in ingredients:
        base = ing.id
        if base in seen:
            seen[base] += 1
            ing.id = f"{base}-{seen[base]}"
        else:
            seen[base] = 1


# Ingredients linked per recipe. Each one compiles a pattern (about 2 ms, for
# the unit list) and scans every step; a hostile page lists thousands, and
# no real recipe comes near this.
MAX_LINKED = 200


def link_references(steps: list[Step], ingredients: list[Ingredient]) -> None:
    """Replace "2 tbsp butter" inside a step with {butter} so it scales with the slider."""
    if not steps:
        return
    for ing in ingredients[:MAX_LINKED]:
        if ing.amount is None:
            continue
        item = re.escape(ing.item)
        pattern = re.compile(
            rf"\b{_NUM}\s*(?:{_UNITS})?\.?\s*(?:of\s+)?(?:the\s+)?{item}\b",
            re.I,
        )
        for step in steps:
            new, n = pattern.subn("{" + ing.id + "}", step.text, count=1)
            if n:
                step.text = new


def _servings(value: str) -> int | None:
    n = int(value) if len(value) <= MAX_NUMBER else MAX_SERVINGS
    return min(n, MAX_SERVINGS) if n > 0 else None


def parse_text(text: str) -> Draft:
    # Runs of spaces fold to one: several patterns here have two optional \s*
    # in a row, which try every split of a run (50,000 spaces cost minutes).
    raw_lines = [" ".join(MARKDOWN_RE.sub("", l).split()) for l in text.replace("\r", "").split("\n")]
    missing: list[str] = []

    title: str | None = None
    base_servings: int | None = None
    prep_min: int | None = None
    cook_min: int | None = None
    total_min: int | None = None
    ingredients: list[Ingredient] = []
    steps: list[Step] = []
    notes: list[str] = []
    group: str | None = None
    mode = "head"  # head | ingredients | steps | notes
    has_headings = any(HEAD_ING.match(l) or HEAD_STEPS.match(l) for l in raw_lines)
    numbered_steps = any(NUMBERED_STEP_RE.match(l) for l in raw_lines)

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue

        if HEAD_ING.match(stripped):
            mode, group = "ingredients", None
            continue
        if HEAD_STEPS.match(stripped):
            mode = "steps"
            continue
        if HEAD_NOTES.match(stripped):
            mode = "notes"
            continue

        # Metadata lines: servings and times. Consume the line if that is all it holds.
        meta_hit = False
        if base_servings is None:
            sm = SERVES_RE.search(stripped) or FOR_PEOPLE_RE.search(stripped) or N_SERVINGS_RE.search(stripped)
            if sm and mode in ("head", "ingredients"):
                base_servings = _servings(sm.group(1))
                meta_hit = True
        for tm in TIME_RE.finditer(stripped):
            kind = tm.group("kind").lower()
            minutes = _duration_minutes(tm.group("dur"))
            if kind.startswith("prep") or kind == "active":
                prep_min = minutes
            elif kind.startswith(("cook", "bake")):
                cook_min = minutes
            elif kind in ("total", "ready in"):
                total_min = minutes
            meta_hit = True
        if meta_hit:
            leftover = TIME_RE.sub("", SERVES_RE.sub("", FOR_PEOPLE_RE.sub("", stripped)))
            if len(re.sub(r"[^A-Za-z]", "", leftover)) < 4:
                continue

        if mode == "notes":
            notes.append(stripped)
            continue

        step_m = NUMBERED_STEP_RE.match(stripped)
        ingredient_like = bool(AMOUNT_RE.match(stripped) and _looks_like_amount_start(stripped)) or bool(
            WORD_AMOUNT_RE.match(stripped)
        )
        if ingredient_like and len(stripped.split()) > 14:
            ingredient_like = False

        if mode == "head":
            digit_led = bool(AMOUNT_RE.match(stripped) and _looks_like_amount_start(stripped))
            if title is None and not digit_led and not step_m and len(stripped.split()) <= 12:
                title = stripped.strip(" :#")[:MAX_TITLE].strip() or None
                if title:
                    continue
            if ingredient_like:
                mode = "ingredients"
            elif step_m or (title is not None and not has_headings):
                mode = "steps" if (step_m or len(stripped.split()) > 6) else "ingredients"

        if mode == "ingredients":
            if step_m and numbered_steps and not ingredient_like:
                mode = "steps"
            elif not ingredient_like and len(stripped.split()) > 6 and not has_headings:
                mode = "steps"
            else:
                gm = GROUP_RE.match(stripped)
                if gm and not ingredient_like and (stripped.endswith(":") or stripped.lower().startswith("for the")):
                    group = gm.group("name").strip().rstrip(":").strip()
                    continue
                ing = parse_ingredient_line(stripped, group)
                if ing:
                    ingredients.append(ing)
                    continue
                if not has_headings:
                    mode = "steps"
                else:
                    gm2 = GROUP_RE.match(stripped)
                    if gm2 and len(stripped.split()) <= 4:
                        group = gm2.group("name").strip()
                    continue

        if mode == "steps":
            if step_m:
                steps.append(Step(text=step_m.group("text").strip(), timer_seconds=_timer_seconds(step_m.group("text"))))
            elif numbered_steps and steps:
                steps[-1].text = f"{steps[-1].text} {stripped}"
                steps[-1].timer_seconds = steps[-1].timer_seconds or _timer_seconds(stripped)
            else:
                step_text = BULLET_RE.sub("", stripped).strip()
                if step_text:  # a bare bullet is not a step
                    steps.append(Step(text=step_text, timer_seconds=_timer_seconds(stripped)))

    if title is None:
        title = "Untitled recipe"
        missing.append("No title found.")
    if base_servings is None:
        base_servings = 4
        missing.append("Servings not found; set to 4.")
    if cook_min is None and total_min is not None:
        cook_min = max(0, total_min - (prep_min or 0))
    if not ingredients:
        missing.append("No ingredient list found.")
    if not steps:
        missing.append("No steps found.")
    no_amount = [i.item for i in ingredients if i.amount is None and i.scaling != "to_taste"]
    if no_amount:
        missing.append("No amount for: " + ", ".join(no_amount) + ".")
    if any(i.estimated for i in ingredients):
        missing.append("Some amounts are estimates.")

    dedupe_ids(ingredients)
    link_references(steps, ingredients)

    recipe = RecipeIn(
        title=title,
        base_servings=base_servings,
        prep_minutes=prep_min,
        cook_minutes=cook_min,
        ingredients=ingredients,
        steps=steps,
        notes="\n".join(notes),
        tags=[],
        source=Source(type="text"),
    )
    return Draft(recipe=recipe, missing=missing, method="local")
