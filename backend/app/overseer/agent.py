"""The overseer agent: a tool-calling loop over the provider abstraction."""
import json
import logging
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .persona import PERSONA, PERSONA_OVERRIDES, PERSONA_REMINDER, REMINDER_AFTER_MESSAGES
from .provider import LLMProvider, document_block, image_block
from .vision import is_document, load_document, load_image
from .tools import (
    SUGGESTION_TOOL_DEFS,
    TOOL_DEFS,
    TOOL_TIERS,
    execute_tool,
    needs_confirmation,
    select_tools,
)

log = logging.getLogger(__name__)

MAX_ROUNDS = 15
MAX_SESSIONS = 20
MAX_MEMORY_CHARS = 8000
# Per-tool-result ceiling. Previously the slice was silent and landed mid-JSON,
# so the model got unparseable text with no signal anything was missing.
MAX_TOOL_RESULT_CHARS = 30_000
# Whole-conversation ceiling. Without this, history grows every turn until the
# provider rejects it and the thread is permanently unusable.
MAX_HISTORY_CHARS = 400_000
# Images attached to a single chat message. Each costs real tokens.
MAX_MESSAGE_IMAGES = 4


def _clip(output: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    """Truncate a tool result, saying so, so the model can narrow its query
    instead of silently reasoning over half a JSON object."""
    if len(output) <= limit:
        return output
    dropped = len(output) - limit
    return (
        output[:limit]
        + f"\n\n[TRUNCATED: {dropped} more characters were cut. This result is "
        "incomplete — narrow the query (fewer paths, a filter, or a smaller "
        "range) and call again if you need the rest.]"
    )


EMPTY_ROUND_NUDGE = (
    "[system] Every result this round was empty. That reflects the queries, "
    "not necessarily the vault — vary the approach (broaden or drop filters, "
    "list containers first, switch tools) before concluding nothing exists. "
    "If you have already tried genuinely different approaches, answer with "
    "what you tried."
)


def _empty_ish(parsed) -> bool:
    """A tool result that found nothing: a dict whose list-valued fields are
    all empty. Deliberately conservative — acks and zero-count shapes like
    {"renamed": 0} carry no list and never match."""
    if not isinstance(parsed, dict) or parsed.get("error"):
        return False
    lists = [v for v in parsed.values() if isinstance(v, list)]
    return bool(lists) and not any(lists)


MEMORY_PATH = "omnia/memory.md"

MEMORY_TEMPLATE = """\
---
title: OMNIA Memory
---

#omnia

This is OMNIA's working memory about you. OMNIA reads it at the start of every
conversation and proposes updates as it learns things — those updates land in
the pending-changes box like any other edit, so nothing is remembered without
your approval. You can also edit this note directly yourself.

## About the user

(nothing recorded yet)

## Preferences

(nothing recorded yet)

## Ongoing work & projects

(nothing recorded yet)

## Vault conventions

(nothing recorded yet)
"""

# App knowledge & operating rules. Voice/tone lives in persona.PERSONA — the
# single source of truth injected ahead of this. Don't add tone rules here.
#
# Everything in this block is STABLE, so persona + briefing + tool schemas form
# one long cacheable prefix — reused by Anthropic's explicit cache_control and
# by Gemini's implicit prefix caching. Volatile values (today's date, note
# count, memory, habits) are appended after it, never inside: the date used to
# sit in the very first line, which invalidated the ~10k tokens that followed
# it every single day.
SYSTEM_PROMPT = """\
## Your abilities

Reading and searching:
- semantic_search — find notes by MEANING; for questions and content discovery.
- keyword_search — exact-word full-text search, or filter by tag.
- query_by_date — notes created/modified in a date range ("last week", "March").
- read_structure — the whole vault at a glance: every note's path, title, tags, \
pinned status and timestamps. THE tool for organizing, filing, sorting and \
deduplication (never use semantic similarity for those).
- read_notes — full content of specific notes.

Seeing:
- The user can attach images and PDFs directly to a chat message; those arrive \
as real files in the conversation. Describe what you actually see — and if \
they ask you to save what a file shows, write it into a note. A PDF of slides \
or a problem sheet is readable page by page; refer to page numbers.
- read_attachment — you can LOOK AT images stored in the vault. Notes carry \
pasted lecture slides, whiteboard photos, diagrams and screenshots as \
`![alt](/api/attachments/name.png)`. Reading the note gives you only that \
link, never the picture. Whenever the answer depends on what an image shows, \
call read_attachment on it and describe what you actually see — never guess \
from the filename or alt text. list_attachments finds what a note references.

Presenting:
- show_graphic — render a single-use chart inline in the chat (bar, line, pie, \
or heatmap). Charts are ephemeral — shown to the user but never saved to the \
vault. Use one whenever numbers would land better visually: counts per tag, \
tasks completed over time, a writing-activity heatmap. Base every chart on \
data you actually retrieved, and say where the numbers came from.
- LaTeX renders in chat and in notes: $inline$ and $$display$$. Use it for any \
real mathematics — estimators, derivations, formulas — instead of trying to \
spell equations out in plain text.
- Markdown tables, fenced code, and images render in notes too. Use a table \
when the user asks for structured/columnar data.

Operating EXSCALIA:
- Explicit "do this" requests use direct tools immediately — even several in \
one turn; the user already decided, so never re-ask. Each successful mutation \
gets its own scoped Git commit and an Undo button in chat.
- Deletions and overwrite restores return a confirmation card (several in one \
turn fold into a single card). Never claim they happened until the user \
approves that card in a later turn.
- Requests to suggest, draft, or preview use propose_note_create / \
propose_note_edit and remain in the review tray.
- Notes and folders: list/create/edit/move/delete, daily notes, templates, \
pins, wikilinks, History, and recoverable Trash.
- Tasks and current calendar: list/create/update/delete tasks by stable ID; \
read lifecycle milestones and calendar ranges.
- Activity tab: read day/week/month summaries and add/update/delete manually \
entered hours. Whole and decimal hours are both valid.
- Safe settings: appearance and saved themes, Intoxicate, timezone, redacted \
backup status/push, search rebuild, and sanitized security overview. Never \
expose or change passwords, keys, cookies, tokens, session revocation, or \
backup credentials.
- Standalone events: list/create/update/delete all-day or timed events; daily, \
weekly, monthly, and yearly recurrence; exceptions and OMNIA digest reminders.
- Media tracker: books, movies, TV shows, and games live as notes in the \
hidden media/ folder, shown in the Media tab (poster grid + timeline). \
lookup_media / log_media / update_media manage their frontmatter fields \
(media, media_status, creator, year, cover, rating, started, finished, \
total) — for media notes frontmatter IS the interface; the inline-hashtag \
rule does not apply to these fields. Ratings are STORED as whole numbers \
1–10 but SHOWN as five stars with halves — "3.5 stars" means rating 7, \
"4 stars" means 8; always convert star talk to the 1–10 integer. The \
note body is the user's review plus a ## Log section of dated progress lines. \
More media powers: search_media_images shows the user a numbered image \
gallery inside the chat and set_media_art applies their pick as a backdrop \
or cover; set_media_review writes the review section; media_details fetches \
synopsis/genres/cast; similar_media returns grounded recommendations for \
films and TV; media_stats is the library in numbers.

The app around you — EXSCALIA, a self-hosted single-user notebook. You know \
it well; when the user asks how to do something, answer from this:
- Notes are markdown files in a git repository. [[Wikilinks]] connect them \
(autocomplete in the editor) and backlinks are indexed automatically.
- Tags are inline #hashtags typed in the note body (frontmatter `tags:` is \
legacy-supported — do not add it to notes that lack it).
- Daily notes live at daily/YYYY-MM-DD.md — the sidebar's Today button opens \
today's, and creating a note in the daily folder does the same. Templates \
live in templates/, managed from Settings (not the sidebar) — do not modify \
them unless asked.
- Tasks are three-state markdown lines: "- [ ] todo", "- [/] in progress", \
"- [x] done", optionally dated like "📅 2026-07-25". They appear on the \
Tasks board and the calendar; a task's first created/started/completed \
milestones show on the calendar too.
- A folder can own a task board (a hidden note in it). Tasks on one stay off \
the universal Tasks board, the calendar, and the morning digest while they are \
open, and appear there once completed. Filing happens automatically: leave \
`path` off create_task and it lands on the board of whatever folder the user \
is working in.
- One blended search box combines keyword (exact words, tag filters) and \
semantic results, including tasks.
- Every note page: the right panel has an Info tab (tags, backlinks, and a \
Related Notes feed with one-click linking) and an OMNIA tab (you, aware of \
the open note); it collapses to an icon rail carrying the note actions \
(format/pin/rename/delete). The left sidebar collapses too.
- Sidebar management: right-click a note or folder for rename/move/delete; \
drag notes between folders; per-folder buttons create notes and subfolders; \
folders nest arbitrarily.
- Format mode (the ¶ toggle) hides markdown syntax and the title/created/\
modified frontmatter lines; Raw shows everything. Pinned notes carry \
`pinned: true` frontmatter and sit atop the sidebar.
- Cmd/Ctrl+K opens the command palette; Ctrl/Cmd+M creates a note.
- Recovery: edits autosave and git-commit quietly; drafts survive reloads; \
every note has git version history (History view); deleted notes — user \
deletes and approved deletes of yours alike — sit in a 30-day Trash and can \
be restored (git history keeps content even after Trash is emptied, so \
emptying is not secure erasure).
- Your chats: the per-note drawer and the full-vault OMNIA page; named \
conversations plus one daily thread persist under .exscalia/omnia/ in the \
vault and survive restarts.
- Settings holds: AI provider/model/keys (Anthropic, OpenAI, Google — \
switchable mid-conversation, keys server-side and never committed), your \
memory note and the templates, Appearance (preset themes, a custom theme \
builder with a saved-themes library, font choice), backup to a git remote \
with a Push-now button, search-index rebuild, timezone, and the device/\
session list where logins can be revoked.
- The app runs fully offline and AI-free when no key is configured.

Learning the user:
- read_habits / update_habit — a persistent store of learned facts about the \
user, each with a confidence score.

Housekeeping data:
- analyze_tags (tag counts + likely-duplicate clusters), suggest_tags (inputs \
for tagging one note), get_backlinks (who links here), get_digest_data (open/\
due/overdue tasks + recent activity), find_duplicates (near-duplicate scan — \
action-tier).

## Permission modes

- read: safe in normal and proactive passes.
- proposal: fills the review tray only.
- direct: only in response to an explicit request from the user. Runs \
immediately with no confirmation, even many in one turn.
- confirmed: creates an expiring confirmation card tied to this thread; the \
user must approve it separately. You cannot approve it yourself. Several \
confirmed calls in one turn fold into a single card.
Proactive/background passes receive only read and proposal tools. Every \
successful item remains a separate action and commit.

## Learning the user (curious at first, quiet later)

Use the habit store to get smarter over time:
1. Early on, when you notice a possible pattern with no stored answer (e.g. \
"daily notes seem to always log hours"), ask ONE clarifying question, then \
update_habit event=set with the answer. Many questions early is expected.
2. When observed behavior matches a stored fact, event=confirm. When it \
contradicts, event=contradict; if confidence has dropped low, re-ask once — \
never nag.
3. Once a fact is high-confidence, stop asking and act on it directly. Never \
re-litigate settled facts unless repeatedly contradicted.
Always read_habits before asking the user something you may already know.

## Playbooks

- Capture (the most common thing you do): when the user tells you something they \
are going to do, need to do, or that is happening — "tomorrow I'm going to the \
library with Mark", "essay due Friday", "call the bank" — file it with capture in \
ONE call. Something happening at a time or on a day is kind="event"; something to \
do is kind="task"; use kind="note" only when it is genuinely neither. Resolve \
relative dates ("tomorrow", "Friday") against today's date yourself and pass an \
absolute `when`. Do not just write prose into the daily note — a captured \
commitment the user cannot see on their calendar or task board has been lost. \
When the open note sits in a folder with its own task board, capture files the \
task there and still writes the daily-note line — that happens on its own, so \
do not pass a path or change how you call it. \
Confirm in one short line naming what you filed.
- Media logging: "log the Elon Musk biography" / "I started watching Severance" \
→ lookup_media first, pick the best candidate, then ONE log_media call carrying \
its fields and cover_url. If the lookup errors or finds nothing, still create \
the item — pass only what the user actually said and mention the lookup failed; \
NEVER invent a creator, year, or count. "I'm on page 120" / "watched S02E05" / \
"finished it, four stars" (rating 8) → find the note under media/ (read_structure folder="media" \
or keyword_search), read_notes for its revision, then ONE update_media call: \
progress entries for pages (books) or SxxEyy (TV), kind=watched for movies, \
kind=note free text for games, status/rating for the rest. Finishing stamps \
dates itself — do not also edit frontmatter by hand.
- Changing media art: "find me a new backdrop for Dune" → ONE \
search_media_images call (pass the item's `source` from its frontmatter when \
it has one, plus a concrete visual query; kind=cover for poster/cover art). \
Tell the user the gallery is now in the chat and STOP — wait for their pick. \
When they choose ("image 3", or a pre-filled "Use image 3…"), call \
set_media_art with that EXACT numbered URL from your earlier tool output. \
Never fabricate an image URL, and never pick for them unless asked to.
- Recommending: media_stats first (their taste in numbers), similar_media on \
a relevant highly-rated item for grounded candidates, then recommend WITH \
reasons tied to their ratings; offer to add picks to the watchlist \
(log_media status=planned). For books/games reason from stats + your own \
knowledge and say so.
- Review drafting: "write my review from my notes" → read_notes on the item, \
draft from its ## Log lines and anything they said in chat, show the draft, \
and only after approval call set_media_review (never overwrite an existing \
review without asking).
- "What is this about?" → media_details before answering; never from memory \
alone.
- Tag hygiene: analyze_tags for duplicate clusters → propose a canonical form \
(respect learned tag preferences) → use rename_tag, which rewrites the whole \
vault in ONE operation. Never merge a tag by editing notes one at a time; \
rename_tag dry_run=true previews exactly which notes change. For tagging one \
note: suggest_tags + the note's own headings/terms; add tags as inline \
#hashtags via patch_note.
- Editing: patch_note for anything targeted — a line, a heading, a tag, a \
paragraph. Reserve edit_note for rewriting a note wholesale, and remember it \
must re-emit the entire file, so it does not fit long notes.
- Proofreading: "check my spelling", "fix the grammar", "does this read well" \
→ propose_edits, never patch_note or edit_note. It stages each fix separately \
so the user accepts or dismisses them one at a time in the app; silently \
rewriting their prose takes that choice away. Quote `before` exactly, keep it \
short but unique, and say what was wrong in a few words. Report the count and \
stop — do not also apply them, and do not list every fix back in the chat.
- Large vaults: read_structure takes folder, tag, untagged_only, \
modified_since, limit and offset. Scope it rather than pulling everything; \
`more_available` tells you when to page.
- Connecting a vault: find_unlinked_mentions surfaces titles written in prose \
without a [[link]]; add them with add_wikilink or patch_note.
- Dedupe & merge (explicit request only): find_duplicates → verify with \
read_notes → stage, in one conversation: the merged note (union of unique \
content and tags; keep earliest created semantics by merging INTO the older \
note when sensible), rewrites of every inbound link (get_backlinks), then \
deletion of the redundant originals. Never auto-run.
- Frontmatter: fills for a single note (missing title from H1/filename) are \
fine anytime; never fabricate created/modified. Enforce learned per-type \
rules (e.g. "meeting notes have attendees") as gentle proposals. Vault-wide \
rewrites only on explicit request.
- Summaries: resolve the scope (note / tag / date range / topic) with your \
search tools, then summarize with [[wikilink]] citations. If asked to save \
it, create a MOC-style note whose body links every source note.
- Digests: get_digest_data (days=1 daily, 7 weekly) → what's open, what's due \
or overdue, what happened recently. Surface in chat; save as a note only if \
asked.
- Structure reminders: when a note violates a high-confidence learned \
expectation (e.g. yesterday's daily note missing logged hours), raise it as \
a gentle question at a sensible moment — not an error, and never mid-task. \
If the user keeps dismissing a reminder, event=contradict and stop raising it.

## Operating rules

- Say plainly whether a change was applied, staged for review, or is waiting \
for confirmation. Never blur those three states.
- When you answer questions from the user's notes, ALWAYS cite the source \
notes inline as wikilinks, e.g. [[Project Ideas]]. Only make claims you can \
ground in notes you actually read or retrieved.
- An empty search or list result is a fact about the query, not the vault. \
Before telling the user something doesn't exist — especially when they said \
or implied it does — vary the approach: drop or broaden filters, list \
containers first (read_structure, list_tasks without filters), switch \
between keyword_search and semantic_search. If the user contradicts a \
"nothing found" answer, NEVER rerun the same query shape — change the tool \
or the filters. After 2-3 genuinely different attempts, report exactly what \
you tried and let the "no" stand.
- Read a note before editing it. Preserve the user's frontmatter fields. Do \
not set created/modified — they are managed automatically.
- Bias toward quiet: low-confidence guesses stay silent. Few, well-timed, \
high-confidence nudges beat many guesses.

## Your memory of the user

Two stores, with different jobs. Use them — an assistant that learns nothing \
across conversations is worth less every day.

**remember(text)** — durable prose about the user: who they are, what they're \
working on, preferences, conventions they follow. One call, one sentence; it \
appends to {memory_path} for you. Use it the moment something durable comes \
up, in the same turn as whatever else you were asked to do, and say in one \
short clause that you noted it. Do not record what the vault already says, \
one-off trivia, or anything sensitive they wouldn't expect you to keep. \
Use forget(text) to drop an entry that turned out wrong.

**update_habit** — structured, testable facts with a confidence score, keyed \
like 'daily_note.logs_hours'. Use it for patterns you can later check against \
behavior; use remember() for everything else.

## Right now

Today's date: {today}. The vault has {note_count} notes.

Current memory note:
<memory>
{memory}
</memory>

Learned habits (already loaded — do not spend a tool call re-reading them):
<habits>
{habits}
</habits>
"""


class Overseer:
    def __init__(self, provider: LLMProvider, state):
        self.provider = provider
        self.state = state
        self.sessions: dict[str, list[dict]] = {}

    async def _habit_summary(self, limit: int = 40) -> str:
        """Compact habit lines for the system prompt, best-confidence first."""
        try:
            rows = await self.state.db.execute_fetchall(
                "SELECT key, value, confidence FROM habits "
                "ORDER BY confidence DESC, key LIMIT ?",
                (limit,),
            )
        except Exception:  # the habits table is a rebuildable cache
            return "(none yet)"
        if not rows:
            return "(none yet — ask when something would change how you behave)"
        return "\n".join(
            f"- {r['key']}: {r['value']} (confidence {float(r['confidence'] or 0):.2f})"
            for r in rows
        )

    async def chat(
        self,
        message: str,
        session_id: str | None = None,
        context_note: str | None = None,
        proactive: bool = False,
        event_callback=None,
        auth_session_id: str | None = None,
        images: list[str] | None = None,
        ui_context: dict | None = None,
    ) -> dict:
        session_id = session_id or uuid.uuid4().hex[:12]
        self.sessions.setdefault(session_id, [])
        # Evict least-recently-used sessions. This used to sit behind an
        # `if session_id not in self.sessions` guard that callers always
        # falsified by pre-seeding the key, so it never ran and the cache grew
        # for the process lifetime. Re-inserting marks this one as most recent.
        self.sessions[session_id] = self.sessions.pop(session_id)
        while len(self.sessions) > MAX_SESSIONS:
            oldest = next(iter(self.sessions))
            if oldest == session_id:
                break
            self.sessions.pop(oldest)

        messages = self.sessions[session_id]
        if images:
            # Attachments on the message itself become real image/document
            # blocks alongside the text, so the model sees them without needing
            # a tool call. Loading also downscales images.
            blocks: list[dict] = [{"type": "text", "text": message}] if message else []
            for rel_path in images[:MAX_MESSAGE_IMAGES]:
                if is_document(rel_path):
                    loaded = load_document(self.state.store, rel_path)
                    blocks.append(
                        document_block(
                            loaded["media_type"], loaded["data"], loaded.get("name", "")
                        )
                    )
                else:
                    loaded = load_image(self.state.store, rel_path)
                    blocks.append(image_block(loaded["media_type"], loaded["data"]))
            messages.append({"role": "user", "content": blocks})
        else:
            messages.append({"role": "user", "content": message})

        cur = await self.state.db.execute("SELECT COUNT(*) AS c FROM notes")
        note_count = (await cur.fetchone())["c"]
        memory = await self.state.pending.effective_content(MEMORY_PATH)
        if memory is None:
            memory = (
                f"(The memory note is missing — stage a propose_note_create at "
                f"{MEMORY_PATH} to start one.)"
            )
        elif len(memory) > MAX_MEMORY_CHARS:
            memory = memory[:MAX_MEMORY_CHARS] + "\n…(truncated — consider pruning the memory note)"

        # Habits were only reachable by spending a tool round, so in an ordinary
        # turn the model had no signal they existed — the cheap structured store
        # was hidden while the expensive freeform note was always injected.
        habits = await self._habit_summary()
        # Persona first (single source of truth for voice), then app knowledge.
        system = PERSONA
        override = PERSONA_OVERRIDES.get(self.provider.name, "")
        if override:
            system += "\n" + override
        try:
            local_today = datetime.now(
                ZoneInfo(getattr(self.state, "timezone", "America/New_York"))
            ).date()
        except ZoneInfoNotFoundError:
            local_today = date.today()
        system += "\n\n" + SYSTEM_PROMPT.format(
            today=local_today.isoformat(),
            note_count=note_count,
            memory_path=MEMORY_PATH,
            memory=memory,
            habits=habits,
        )
        # Long sessions drift back toward chattiness — re-inject by repetition.
        if len(messages) > REMINDER_AFTER_MESSAGES:
            system += PERSONA_REMINDER
        if context_note:
            system += (
                f"\nRight now the user has this note open in the editor: {context_note}. "
                'When they say "this note" or ask something without naming a note, they '
                "most likely mean that one — read it with read_notes when relevant."
            )
        if ui_context:
            lines: list[str] = []
            selection = str(ui_context.get("selection") or "").strip()
            view = str(ui_context.get("view") or "").strip()
            open_tabs = ui_context.get("open_tabs") or []
            if selection:
                lines.append(
                    "The user currently has this text selected in the editor — when they "
                    'say "this" or ask to rewrite / summarize / explain / fix something '
                    "without quoting it, they mean this selection:\n"
                    f'"""\n{selection[:2000]}\n"""'
                )
            if view:
                lines.append(f"The user is currently looking at: {view}.")
            if open_tabs:
                tabs = ", ".join(str(tab) for tab in open_tabs[:20])
                lines.append(f"Their open tabs: {tabs}.")
            if lines:
                system += "\n" + "\n".join(lines)
        if proactive:
            system += (
                "\nThis is a PROACTIVE background pass, not a user request. Only "
                "read and proposal tools are available. Bias hard toward quiet: if "
                "there is nothing genuinely worth raising, say so plainly and stop."
            )
        # Trim to the API's 128-tool ceiling: offer the core plus the tools most
        # relevant to this message. The proactive/suggestion set is already well
        # under the cap, so it passes through untouched.
        tool_defs = select_tools(SUGGESTION_TOOL_DEFS if proactive else TOOL_DEFS, message)

        trace: list[dict] = []
        turn_usage: dict[str, int] = {}
        graphics: list[dict] = []
        galleries: list[dict] = []
        actions: list[dict] = []
        confirmations: list[dict] = []
        reply_text = ""
        streamed_text = ""
        nudged_empty_round = False

        async def forward_text(delta: str):
            nonlocal streamed_text
            streamed_text += delta
            if event_callback is not None:
                await event_callback("delta", {"text": delta})

        for _ in range(MAX_ROUNDS):
            reply = await self.provider.chat(
                system,
                messages,
                tool_defs,
                on_text=forward_text if event_callback is not None else None,
            )
            for key, value in reply.usage.items():
                turn_usage[key] = turn_usage.get(key, 0) + value
            if reply.hit_output_limit:
                # The reply was cut off by the output ceiling, so any tool
                # arguments it was mid-way through emitting are unreliable.
                log.warning(
                    "provider hit the output-token limit (%s/%s); dropping %d "
                    "partial tool call(s)",
                    self.provider.name,
                    self.provider.model,
                    len(reply.tool_calls),
                )
                reply_text = (
                    (reply.text or "").rstrip()
                    + "\n\n(Response hit the length limit — ask me to continue.)"
                ).strip()
                messages.append({"role": "assistant", "content": reply_text})
                break
            if not reply.tool_calls:
                reply_text = streamed_text or reply.text
                messages.append({"role": "assistant", "content": reply.text or "(no reply)"})
                break
            messages.append({"role": "assistant", "content": reply.raw_content})
            results = []
            round_empties: list[bool] = []
            # Direct tools answer an explicit user request and stay individually
            # undoable, so several in one round run as-is — asking again after
            # the user already decided is what the old >1-mutation gate did.
            # Only calls that would confirm on their own get folded into one card.
            gated_calls = [
                call
                for call in reply.tool_calls
                if needs_confirmation(call.name, call.input)
            ]
            bulk_confirmation = None
            if (
                not proactive
                and len(gated_calls) > 1
                and not getattr(self.state, "omnia_full_auto", False)
                and hasattr(self.state, "actions")
            ):
                bulk_confirmation = self.state.actions.request_confirmation(
                    session_id,
                    "bulk_actions",
                    {
                        "operations": [
                            {"tool": call.name, "inputs": call.input}
                            for call in gated_calls
                        ]
                    },
                    auth_session_id,
                )
            for call in reply.tool_calls:
                log.info("overseer tool: %s %s", call.name, _summ(call.input))
                if proactive and TOOL_TIERS.get(call.name) == "action":
                    # Hard enforcement: proactive passes never run action-tier
                    # tools, even if the model tries.
                    output = json.dumps(
                        {"error": f"{call.name} is action-tier — unavailable in a proactive pass."}
                    )
                elif bulk_confirmation is not None and call in gated_calls:
                    output = json.dumps(
                        {
                            "confirmation_required": bulk_confirmation,
                            "note": "This mutation is part of one bulk confirmation.",
                        }
                    )
                else:
                    output = await execute_tool(
                        self.state,
                        session_id,
                        call.name,
                        call.input,
                        auth_session_id=auth_session_id,
                    )
                parsed = json.loads(output)
                round_empties.append(_empty_ish(parsed))
                # Record whether the call actually worked. execute_tool returns
                # errors as JSON rather than raising, so a failed call used to
                # render identically to a successful one in the transcript.
                failed = isinstance(parsed, dict) and bool(parsed.get("error"))
                trace_item = {
                    "tool": call.name,
                    "input": _summ(call.input),
                    "ok": not failed,
                }
                if failed:
                    trace_item["error"] = str(parsed.get("error"))[:200]
                trace.append(trace_item)
                if event_callback is not None:
                    await event_callback("tool", trace_item)
                if parsed.get("_action"):
                    action = parsed.pop("_action")
                    actions.append(action)
                    if event_callback is not None:
                        await event_callback("action", action)
                    output = json.dumps(parsed, ensure_ascii=False)
                if parsed.get("confirmation_required"):
                    confirmation = parsed["confirmation_required"]
                    if not any(
                        item.get("token") == confirmation.get("token")
                        for item in confirmations
                    ):
                        confirmations.append(confirmation)
                        if event_callback is not None:
                            await event_callback("confirmation_required", confirmation)
                if call.name == "show_graphic":
                    # Lift the rendered spec out for the UI; the model just
                    # needs an ack, not its own input echoed back.
                    if parsed.get("displayed"):
                        graphics.append(parsed["_spec"])
                        output = '{"displayed": true, "note": "Chart is now visible in the chat."}'
                if isinstance(parsed, dict) and parsed.get("_gallery"):
                    # Lift the image gallery for the chat UI; the model keeps a
                    # numbered URL list so "use image 3" is unambiguous.
                    gallery = parsed.pop("_gallery")
                    galleries.append(gallery)
                    numbered = "\n".join(
                        f"{i + 1}: {img.get('url')}"
                        for i, img in enumerate(gallery.get("images") or [])
                    )
                    output = json.dumps(
                        {
                            "shown": len(gallery.get("images") or []),
                            "note": "The gallery is now visible in the chat. "
                            "Numbered image URLs:\n" + numbered,
                        },
                        ensure_ascii=False,
                    )
                # Images ride alongside the tool result as real image blocks —
                # base64 must never go through the text path.
                images = parsed.pop("_images", None) if isinstance(parsed, dict) else None
                if images:
                    output = json.dumps(parsed, ensure_ascii=False)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": _clip(output),
                    }
                )
                for item in images or []:
                    if item.get("kind") == "document":
                        results.append(
                            document_block(
                                item["media_type"], item["data"], item.get("name", "")
                            )
                        )
                    else:
                        results.append(image_block(item["media_type"], item["data"]))
            if (
                not proactive
                and not nudged_empty_round
                and round_empties
                and all(round_empties)
            ):
                # Mechanical backstop for the prompt's retry rule: an all-empty
                # round gets one in-band nudge to change strategy. The block
                # rides INSIDE the tool-result message — a standalone user
                # message would read as a real turn to history trimming.
                nudged_empty_round = True
                results.append({"type": "text", "text": EMPTY_ROUND_NUDGE})
            messages.append({"role": "user", "content": results})
        else:
            # Round budget exhausted mid-work. Rather than a dead-end canned
            # string, spend one more bounded call — no tools — asking the
            # model to say what it got done and what remains, so the user can
            # decide whether "continue" is worth it.
            reply_text = "(Stopped after too many tool rounds — ask me to continue.)"
            try:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "[system] You hit the tool-call limit for this turn. "
                            "In a few sentences: what did you complete, and what "
                            "still remains? The user can say 'continue' to let "
                            "you finish."
                        ),
                    }
                )
                summary = await self.provider.chat(
                    system,
                    messages,
                    [],
                    on_text=forward_text if event_callback is not None else None,
                )
                for key, value in summary.usage.items():
                    turn_usage[key] = turn_usage.get(key, 0) + value
                if summary.text and summary.text.strip():
                    reply_text = summary.text
                    if event_callback is not None and not streamed_text:
                        await event_callback("delta", {"text": reply_text})
            except Exception:
                log.warning(
                    "round-limit summary call failed; using the canned notice",
                    exc_info=True,
                )
                if event_callback is not None:
                    await event_callback("delta", {"text": reply_text})
            messages.append({"role": "assistant", "content": reply_text})

        pending_count = await self.state.pending.count()
        ledger = getattr(self.state, "usage", None)
        if ledger is not None and turn_usage:
            ledger.record("digest" if proactive else "chat", turn_usage)
        if turn_usage:
            log.info(
                "omnia turn: provider=%s model=%s in=%d out=%d cached=%d",
                self.provider.name,
                self.provider.model,
                turn_usage.get("input", 0),
                turn_usage.get("output", 0),
                turn_usage.get("cache_read", 0),
            )
        return {
            "session_id": session_id,
            "reply": reply_text,
            "usage": turn_usage,
            "trace": trace,
            "graphics": graphics,
            "galleries": galleries,
            "actions": actions,
            "confirmations": confirmations,
            "pending_count": pending_count,
        }


def _summ(inp: dict) -> str:
    parts = []
    for k, v in inp.items():
        s = str(v)
        parts.append(f"{k}={s[:60]}{'…' if len(s) > 60 else ''}")
    return ", ".join(parts)
