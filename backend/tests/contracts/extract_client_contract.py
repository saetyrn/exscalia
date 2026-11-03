"""Write the API calls a shipped client makes as a contract fixture.

Run from backend/:

    uv run python tests/contracts/extract_client_contract.py --ref v0.1.29 --source desktop
    uv run python tests/contracts/extract_client_contract.py --source ipad
    ... --check    # regenerate in memory, diff against the committed file, exit 1 on drift

``desktop`` reads every file under frontend/src at REF that contains ``/api/``
(the desktop app bundles the SPA as it was at its release tag). ``ipad`` reads
the iPad's Swift sources that contain ``/api/`` at REF (default HEAD); nearly
all of it is PaperCore's PaperAPI.swift. The output is
``<source>-<ref>.json`` next to this script: a sorted list of
``{method, path_template, source}``. test_released_contract.py checks each
entry against the routes the app registers now.

Regenerate the desktop fixture when tagging a release, and the iPad fixture
when a new iPad build is installed. Delete a fixture once no client of that
version is still in use.

How literals are read. Each ``/api/...`` string or template literal is one
entry. Interpolations (``${...}``, ``\\(...)``, a ``+`` operand) become
``{param}``, one per run of adjacent ones. Everything from a query string on
is dropped. A literal that ends in ``/`` is a prefix the client completes at
runtime (``startsWith('/api/attachments/')`` guarding an image it loads), so
it gets a trailing ``{param}``. The method is the ``method:`` of the nearest
enclosing call that names one, or a private Swift helper such as ``postJSON``
whose body fixes one; for ``const path = `/api/…` `` with no call around it,
the method of the first ``(path, { method: … })`` after it; GET otherwise.
Literals that name no route (a bare ``/api/`` prefix test,
``/api/${vaultPath}``), a whole path passed straight to a string test
(``startsWith('/api/auth/login')``; a prefix ending in ``/`` stays, as
above) and an error label (``toApiError(res, '/api/attachments')``) are
listed on stderr and left out.

This reads source text and is not a parser. A URL built somewhere other than
the literal (a variable passed to ``req`` later with its own ``method:``) gets
the wrong method, so read the diff when you regenerate.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent

# source -> (pathspec at the ref, file suffixes that ship)
SOURCES = {
    "desktop": ("frontend/src", (".ts", ".js", ".svelte")),
    "ipad": ("ipad", (".swift",)),
}
# Tests and test helpers are not part of the shipped client.
TEST_PATH = re.compile(r"(^|/)(tests?|testing|\w*Tests)(/|$)|\.(test|spec)\.[jt]s$|Tests\.swift$")

ROUTE_LIKE = re.compile(r"^/api/[A-Za-z][\w\-./{}]*$")
METHOD_KEY = re.compile(r"""method\s*:\s*(['"])([A-Za-z]+)\1""")
PLUS = re.compile(r"\s*\+\s*")
WORD_CHAIN = re.compile(r"[A-Za-z_$][\w$.]*")
# After one of these a `/` starts a regex literal rather than dividing.
REGEX_AFTER = set("(,=:[!&|?{};+-*%~^")
# A literal passed straight to one of these is compared or quoted, not
# requested; a string test of a prefix ending in `/` still counts (above).
STRING_TESTS = {"startsWith", "endsWith", "includes"}
ERROR_LABELS = {"toApiError"}
ASSIGNED_TO = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*$")
REGEX_KEYWORDS = {
    "return", "typeof", "case", "in", "of", "void",
    "yield", "await", "delete", "throw", "else", "do",
}


@dataclass
class Literal:
    start: int
    end: int
    # ("text", chars) and ("expr", interpolated source), in order.
    parts: list[tuple[str, str]]
    # Positions of the brackets open around the literal, outermost first.
    stack: tuple[int, ...]


@dataclass
class Scan:
    src: str
    swift: bool
    literals: list[Literal] = field(default_factory=list)
    # (position, METHOD, bracket stack) for every `method: "X"` in code.
    methods: list[tuple[int, str, tuple[int, ...]]] = field(default_factory=list)
    # open bracket position -> its closing position
    pairs: dict[int, int] = field(default_factory=dict)
    script: list[tuple[int, int]] | None = None
    stack: list[int] = field(default_factory=list)

    def run(self) -> "Scan":
        self._code(0, None)
        self.literals.sort(key=lambda lit: lit.start)
        return self

    def _code(self, i: int, closer: str | None) -> int:
        """Scan code from ``i``; return the index of ``closer`` at this depth."""
        src, n, base = self.src, len(self.src), len(self.stack)
        while i < n:
            c = src[i]
            if src.startswith("//", i):
                j = src.find("\n", i)
                i = n if j < 0 else j
                continue
            if src.startswith("/*", i):
                j = src.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            if c == '"' or (c == "'" and not self.swift):
                i = self._string(i, c)
                continue
            if c == "`" and not self.swift:
                i = self._template(i)
                continue
            if c == "/" and not self.swift and self._regex_start(i):
                j = self._regex_end(i)
                if j:
                    i = j
                    continue
            if c in "([{":
                self.stack.append(i)
            elif c in ")]}":
                if len(self.stack) > base:
                    self.pairs[self.stack.pop()] = i
                elif c == closer:
                    return i
            elif c == "m" and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] in "_$.")):
                m = METHOD_KEY.match(src, i)
                if m:
                    self.methods.append((i, m.group(2).upper(), tuple(self.stack)))
            i += 1
        return n

    def _interpolation(self, open_at: int) -> int:
        """Scan the expression after the bracket at ``open_at``; return its close."""
        closer = ")" if self.src[open_at] == "(" else "}"
        self.stack.append(open_at)
        end = self._code(open_at + 1, closer)
        self.stack.pop()
        self.pairs[open_at] = end
        return end

    def _string(self, i: int, quote: str) -> int:
        src, n = self.src, len(self.src)
        if self.swift and src.startswith('"""', i):
            j = src.find('"""', i + 3)
            end = n if j < 0 else j + 3
            self._add(i, end, [("text", src[i + 3 : end - 3])])
            return end
        parts: list[tuple[str, str]] = []
        buf: list[str] = []
        j = i + 1
        # Quoted strings never span lines, so a stray quote costs one line.
        while j < n and src[j] not in (quote, "\n"):
            if src[j] == "\\":
                if self.swift and src.startswith("\\(", j):
                    parts.append(("text", "".join(buf)))
                    buf = []
                    end = self._interpolation(j + 1)
                    parts.append(("expr", src[j + 2 : end]))
                    j = end + 1
                    continue
                buf.append(src[j : j + 2])
                j += 2
                continue
            buf.append(src[j])
            j += 1
        parts.append(("text", "".join(buf)))
        self._add(i, j + 1, parts)
        return j + 1

    def _template(self, i: int) -> int:
        src, n = self.src, len(self.src)
        parts: list[tuple[str, str]] = []
        buf: list[str] = []
        j = i + 1
        while j < n and src[j] != "`":
            if src[j] == "\\":
                buf.append(src[j : j + 2])
                j += 2
                continue
            if src.startswith("${", j):
                parts.append(("text", "".join(buf)))
                buf = []
                end = self._interpolation(j + 1)
                parts.append(("expr", src[j + 2 : end]))
                j = end + 1
                continue
            buf.append(src[j])
            j += 1
        parts.append(("text", "".join(buf)))
        self._add(i, j + 1, parts)
        return j + 1

    def _add(self, start: int, end: int, parts: list[tuple[str, str]]) -> None:
        self.literals.append(Literal(start, end, parts, tuple(self.stack)))

    def _regex_start(self, i: int) -> bool:
        if self.script is not None and not any(a <= i < b for a, b in self.script):
            return False  # svelte markup: `</div>`, `/>`
        k = i - 1
        while k >= 0 and self.src[k] in " \t\r\n":
            k -= 1
        if k < 0 or self.src[k] in REGEX_AFTER:
            return True
        word = re.search(r"[A-Za-z_$][\w$]*$", self.src[max(0, k - 11) : k + 1])
        return bool(word) and word.group() in REGEX_KEYWORDS

    def _regex_end(self, i: int) -> int | None:
        src, n, j, in_class = self.src, len(self.src), i + 1, False
        while j < n and src[j] != "\n":
            ch = src[j]
            if ch == "\\":
                j += 2
                continue
            if in_class:
                in_class = ch != "]"
            elif ch == "[":
                in_class = True
            elif ch == "/":
                j += 1
                while j < n and src[j].isalpha():
                    j += 1
                return j
            j += 1
        return None  # no closing slash on the line: it was a division


def _callee(src: str, paren: int) -> str:
    m = re.search(r"([A-Za-z_$][\w$]*)\s*$", src[max(0, paren - 80) : paren])
    return m.group(1) if m else ""


def swift_helpers(scan: Scan) -> dict[str, str]:
    """Private Swift helpers whose body pins one method, e.g. postJSON -> POST."""
    src, helpers = scan.src, {}
    for m in re.finditer(r"\b(?:private|fileprivate)\s+func\s+(\w+)", src):
        params = src.find("(", m.end())
        if params < 0 or params not in scan.pairs:
            continue
        body = src.find("{", scan.pairs[params])
        if body < 0 or body not in scan.pairs:
            continue
        found = {meth for pos, meth, _ in scan.methods if body < pos < scan.pairs[body]}
        if len(found) == 1:
            helpers[m.group(1)] = found.pop()
    return helpers


def method_for(lit: Literal, scan: Scan, helpers: dict[str, str]) -> str:
    """The method of the nearest enclosing call that names or implies one."""
    src = scan.src
    for depth in range(len(lit.stack) - 1, -1, -1):
        paren = lit.stack[depth]
        if src[paren] != "(":
            continue
        prefix = lit.stack[: depth + 1]
        for _, meth, stack in scan.methods:
            if stack[: depth + 1] != prefix:
                continue
            # A call argument (Swift `method:`) or a key of an object argument
            # (`req(url, { method: 'POST' })`), not one nested in the body.
            inner = stack[depth + 1 :]
            if not inner or (len(inner) == 1 and src[inner[0]] == "{"):
                return meth
        name = _callee(src, paren)
        if name in helpers:
            return helpers[name]
    return _method_of_variable(lit, scan) or "GET"


def _method_of_variable(lit: Literal, scan: Scan) -> str | None:
    """``const path = `/api/…`; … apiFetch(path, { method: 'POST' })``."""
    if lit.stack and scan.src[lit.stack[-1]] != "{":
        return None
    m = ASSIGNED_TO.search(scan.src[max(0, lit.start - 80) : lit.start])
    if m is None:
        return None
    use = re.compile(rf"\(\s*{re.escape(m.group(1))}\s*,\s*\{{").search(scan.src, lit.end)
    if use is None:
        return None
    options = use.end() - 1
    return next((meth for _, meth, stack in scan.methods if stack and stack[-1] == options), None)


def normalise(parts: list[tuple[str, str]]) -> tuple[str, bool]:
    """Path template of a literal's parts, and whether a query string ended it."""
    out: list[str] = []
    for kind, value in parts:
        if kind == "text":
            cut = re.search(r"[?#]", value)
            if cut:
                out.append(value[: cut.start()])
                return "".join(out), True
            out.append(value)
        elif re.search(r"""[`'"][?&#]""", value):
            return "".join(out), True  # `${qs ? `?${qs}` : ''}`
        elif not "".join(out).endswith("{param}"):  # `${id}${suffix}` fills one slot
            out.append("{param}")
    return "".join(out), False


def _operand_end(scan: Scan, i: int, by_start: dict[int, Literal]) -> int | None:
    if i in by_start:
        return by_start[i].end
    m = WORD_CHAIN.match(scan.src, i)
    j = m.end() if m else i
    while j < len(scan.src) and scan.src[j] in "([" and j in scan.pairs:
        j = scan.pairs[j] + 1
    return j if j > i else None


def concatenated(scan: Scan, lit: Literal, path: str, by_start: dict[int, Literal]) -> str:
    """Extend ``path`` over `` + operand`` chains after the literal."""
    i = lit.end
    while m := PLUS.match(scan.src, i):
        start = m.end()
        end = _operand_end(scan, start, by_start)
        if end is None:
            break
        inner = [x for x in scan.literals if start <= x.start < end]
        if len(inner) == 1:
            # "…/blob/" + Self.encode(path: "\(notebook)/\(pageID)/\(name)")
            text, stopped = normalise(inner[0].parts)
            path += text
            if stopped:
                break
        elif not path.endswith("{param}"):
            path += "{param}"
        i = end
    return path


def extract(src: str, source: str) -> tuple[list[dict], list[str]]:
    """Entries for one file, and the /api literals it left out."""
    script = None
    if source.endswith(".svelte"):
        src = re.sub(r"<!--.*?-->", lambda m: " " * len(m.group()), src, flags=re.S)
        blocks = re.finditer(r"<script[^>]*>(.*?)</script>", src, re.S)
        script = [(m.start(1), m.end(1)) for m in blocks]
    scan = Scan(src, source.endswith(".swift"), script=script).run()
    # Private helpers are file-scoped, so each file's own are the only ones it can call.
    helpers = swift_helpers(scan) if scan.swift else {}
    by_start = {lit.start: lit for lit in scan.literals}
    entries, skipped = [], []
    for lit in scan.literals:
        parts = lit.parts
        if (
            len(parts) >= 3
            and parts[0] == ("text", "")
            and parts[1][0] == "expr"
            and parts[2][1].startswith("/api/")
        ):
            parts = parts[2:]  # `${apiBase()}/api/live`
        if not parts[0][1].startswith("/api/"):
            continue
        callee = _callee(src, lit.stack[-1]) if lit.stack and src[lit.stack[-1]] == "(" else ""
        if callee in ERROR_LABELS or (callee in STRING_TESTS and not parts[-1][1].endswith("/")):
            skipped.append(f"{source}: {src[lit.start : lit.end]}")
            continue
        path, stopped = normalise(parts)
        if not stopped:
            path = concatenated(scan, lit, path, by_start)
        if path.endswith("/"):
            path += "{param}"
        if not ROUTE_LIKE.match(path):
            skipped.append(f"{source}: {src[lit.start : lit.end]}")
            continue
        entries.append(
            {"method": method_for(lit, scan, helpers), "path_template": path, "source": source}
        )
    return entries, skipped


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout


def contract(ref: str, source: str) -> tuple[list[dict], list[str]]:
    pathspec, suffixes = SOURCES[source]
    listed = _git("grep", "-l", "/api/", ref, "--", pathspec).splitlines()
    files = [line.split(":", 1)[1] for line in listed]
    entries, skipped = [], []
    for path in files:
        if not path.endswith(suffixes) or TEST_PATH.search(path):
            continue
        found, left_out = extract(_git("show", f"{ref}:{path}"), path)
        entries += found
        skipped += left_out
    unique = {(e["path_template"], e["method"], e["source"]): e for e in entries}
    return [unique[key] for key in sorted(unique)], skipped


def default_out(ref: str, source: str) -> Path:
    name = "head" if ref == "HEAD" else re.sub(r"[^\w.\-]+", "-", ref)
    return HERE / f"{source}-{name}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", choices=sorted(SOURCES), required=True)
    parser.add_argument("--ref", default="HEAD", help="git ref to read (default HEAD)")
    parser.add_argument("--out", type=Path, help="default: <source>-<ref>.json next to this script")
    parser.add_argument(
        "--check", action="store_true", help="diff against the committed file instead of writing"
    )
    args = parser.parse_args(argv)

    entries, skipped = contract(args.ref, args.source)
    for line in skipped:
        print(f"left out (names no route, or is not requested): {line}", file=sys.stderr)
    text = json.dumps(entries, indent=1) + "\n"
    out = args.out or default_out(args.ref, args.source)
    if args.check:
        committed = out.read_text() if out.exists() else ""
        if committed == text:
            print(f"{out.name}: up to date ({len(entries)} calls)")
            return 0
        sys.stdout.writelines(
            difflib.unified_diff(
                committed.splitlines(True),
                text.splitlines(True),
                f"{out.name} (committed)",
                f"{out.name} ({args.ref})",
            )
        )
        return 1
    out.write_text(text)
    print(f"wrote {out.name} ({len(entries)} calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
