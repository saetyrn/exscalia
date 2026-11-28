"""Unit aliases and the words that decide an ingredient's scaling rule."""
from __future__ import annotations

import re

# Canonical unit -> spellings seen in recipes. Single letters are case-sensitive (T = tbsp, t = tsp).
UNIT_ALIASES: dict[str, list[str]] = {
    "tsp": ["tsp", "tsp.", "tsps", "teaspoon", "teaspoons", "t"],
    "tbsp": ["tbsp", "tbsp.", "tbsps", "tbs", "tbs.", "tablespoon", "tablespoons", "T", "Tbsp", "Tbs"],
    "cup": ["cup", "cups", "c", "c."],
    "ml": ["ml", "mL", "ml.", "milliliter", "milliliters", "millilitre", "millilitres"],
    "l": ["l", "L", "liter", "liters", "litre", "litres"],
    "g": ["g", "g.", "gr", "gram", "grams", "gm"],
    "kg": ["kg", "kg.", "kilo", "kilos", "kilogram", "kilograms"],
    "oz": ["oz", "oz.", "ounce", "ounces"],
    "lb": ["lb", "lbs", "lb.", "lbs.", "pound", "pounds"],
    "pinch": ["pinch", "pinches"],
    "clove": ["clove", "cloves"],
    "slice": ["slice", "slices"],
    "can": ["can", "cans", "tin", "tins"],
    "bunch": ["bunch", "bunches"],
    "stick": ["stick", "sticks"],
    "sprig": ["sprig", "sprigs"],
}

_CASE_SENSITIVE = {"t", "T", "c", "l", "L"}
_ALIAS_LOOKUP: dict[str, str] = {}
for _unit, _aliases in UNIT_ALIASES.items():
    for _a in _aliases:
        _ALIAS_LOOKUP[_a if _a in _CASE_SENSITIVE else _a.lower()] = _unit


def unit_from_word(word: str) -> str | None:
    """Return the canonical unit for a word, or None if it is not a unit."""
    if word in _ALIAS_LOOKUP and word in _CASE_SENSITIVE:
        return _ALIAS_LOOKUP[word]
    return _ALIAS_LOOKUP.get(word.lower()) if word not in _CASE_SENSITIVE else None


def unit_alias_pattern() -> str:
    words = sorted({a for aliases in UNIT_ALIASES.values() for a in aliases}, key=len, reverse=True)
    return "|".join(re.escape(w) for w in words)


# Items scaled as a starting point rather than exactly.
TO_TASTE_RE = re.compile(
    r"\b(salt|pepper|peppercorns?|chil+i\s+flakes|red\s+pepper\s+flakes|cayenne|paprika|cumin|"
    r"coriander|oregano|thyme|rosemary|basil|cinnamon|nutmeg|cloves?\s+\(ground\)|allspice|"
    r"turmeric|curry\s+powder|garam\s+masala|seasoning|spices?|hot\s+sauce|sriracha|tabasco|"
    r"garlic\s+powder|onion\s+powder|msg|five[- ]spice|za'?atar|sumac|herbs?)\b",
    re.IGNORECASE,
)

# Counted items that only make sense as whole numbers.
WHOLE_RE = re.compile(
    r"\b(eggs?|egg\s+(yolks?|whites?)|tortillas?|buns?|rolls?|sausages?|"
    r"chicken\s+(thighs?|breasts?|drumsticks?|wings?|legs?)|fil+ets?|steaks?|chops?|"
    r"pitas?|wraps?|sheets?|bay\s+lea(f|ves)|cans?|patties|patty|burgers?|hot\s+dogs?)\b",
    re.IGNORECASE,
)


def default_scaling(unit: str, item: str) -> str:
    if TO_TASTE_RE.search(item) and unit in ("", "tsp", "tbsp", "pinch"):
        return "to_taste"
    if unit == "" and WHOLE_RE.search(item):
        return "whole"
    return "linear"
