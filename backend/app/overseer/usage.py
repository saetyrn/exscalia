"""Token-usage ledger and the soft monthly budget.

Every model call in the app — chat turns, the morning digest, nightly
housekeeping — records what it burned, per UTC day and per caller, into
`.exscalia/omnia/usage.json`. That lives inside the vault on purpose: it is
numbers only, so git-backing it is safe, and it rides the same autocommit and
off-site backup the threads already do.

The budget is deliberately soft and asymmetric: when a month's spend crosses
it, unattended work (housekeeping) pauses first, while interactive chat keeps
answering — a ceiling that silences the user's own questions would just get
turned off.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..vault.internal import atomic_json, read_json
from ..vault.notes import NoteError, NoteStore

VERSION = 1
# Days of history kept. Enough for a year-over-year look without the file
# growing forever; at ~4 callers × 5 ints a day this stays a few kilobytes.
KEEP_DAYS = 400

# The budget counts what costs real money, in plain unweighted tokens:
# full-price prompt tokens, completion tokens and cache writes (billed at
# 1.25x input). With Anthropic breakpoints on the tools, the system prompt and
# the history tail, most of a prompt is a write or a read, so leaving writes
# out made the total almost output-only. Cache reads bill at a tenth or less
# and stay out: they would drown the signal.
BUDGET_KEYS = ("input", "output", "cache_write")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class UsageLedger:
    def __init__(self, vault_dir: Path):
        self.path = vault_dir / NoteStore.INTERNAL_DIR / "omnia" / "usage.json"

    def _load(self) -> dict:
        try:
            value = read_json(self.path, None)
        except NoteError:
            # read_json already set the unreadable file aside; a ledger is
            # bookkeeping, so start a fresh one rather than blocking anything.
            value = None
        if not isinstance(value, dict) or not isinstance(value.get("days"), dict):
            return {"version": VERSION, "days": {}}
        return value

    def record(self, caller: str, usage: dict | None) -> None:
        """Fold one call's usage into today's row. Never raises — a ledger
        write must not fail the model call it is accounting for."""
        if not usage:
            return
        try:
            data = self._load()
            days = data["days"]
            row = days.setdefault(_today(), {}).setdefault(caller, {})
            for key in ("input", "output", "cache_read", "cache_write"):
                amount = usage.get(key)
                if isinstance(amount, (int, float)) and amount > 0:
                    row[key] = int(row.get(key, 0)) + int(amount)
            row["calls"] = int(row.get("calls", 0)) + 1
            if len(days) > KEEP_DAYS:
                for day in sorted(days)[: len(days) - KEEP_DAYS]:
                    del days[day]
            atomic_json(self.path, data)
        except Exception:  # pragma: no cover - defensive
            import logging

            logging.getLogger(__name__).warning(
                "usage ledger write failed", exc_info=True
            )

    def month_summary(self, month: str | None = None) -> dict:
        """Totals for one month (UTC), overall and per caller."""
        month = month or _today()[:7]
        by_caller: dict[str, dict] = {}
        for day, callers in self._load()["days"].items():
            if not day.startswith(month) or not isinstance(callers, dict):
                continue
            for caller, row in callers.items():
                if not isinstance(row, dict):
                    continue
                out = by_caller.setdefault(caller, {})
                for key, amount in row.items():
                    if isinstance(amount, (int, float)):
                        out[key] = int(out.get(key, 0)) + int(amount)
        total = sum(
            row.get(key, 0) for row in by_caller.values() for key in BUDGET_KEYS
        )
        return {"month": month, "total": total, "by_caller": by_caller}

    def allow_background(self, budget: int) -> bool:
        """May unattended work spend right now? 0 or less means no budget."""
        if budget <= 0:
            return True
        return self.month_summary()["total"] < budget
