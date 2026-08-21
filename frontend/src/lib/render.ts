// Read mode: full markdown → sanitized HTML. This is the only place notes are
// rendered as a finished document rather than as source.
import DOMPurify from 'dompurify';
import { Marked } from 'marked';
import { encodePath } from './api';
import { renderMathInHtml } from './math';

const marked = new Marked({ gfm: true, breaks: false });

/** Pull math out before Marked runs so it can't mangle TeX (e.g. `_` as
 * emphasis, `\\` as a break), then put rendered KaTeX back afterwards. */
function protectMath(src: string): { text: string; restore: (html: string) => string } {
	const stash: string[] = [];
	const text = src.replace(
		/\$\$[\s\S]+?\$\$|\$(?!\s)(?:[^$\n\\]|\\.)+?(?<!\s)\$/g,
		(match) => {
			stash.push(match);
			// Alphanumeric token: survives inline formatting untouched.
			return `xmathholderx${stash.length - 1}x`;
		}
	);
	const restore = (html: string) =>
		html.replace(/xmathholderx(\d+)x/g, (_, i) => renderMathInHtml(stash[Number(i)] ?? ''));
	return { text, restore };
}

export interface RenderOptions {
	/** Resolve a wikilink target to a note path, or null when it doesn't exist. */
	resolveNote?: (name: string) => string | null;
	/** Demote headings by N levels — chat replies shouldn't emit page-level h1s. */
	headingOffset?: number;
	/** Strip YAML frontmatter first (true for notes, false for chat). */
	stripMeta?: boolean;
}

/** Markdown → sanitized HTML. Shared by Read mode and OMNIA's chat so both
 * support the same syntax: tables, fenced code, lists, links, quotes, math. */
export function renderMarkdown(source: string, options: RenderOptions = {}): string {
	const body = options.stripMeta ? preserveIndentation(stripFrontmatter(source)) : source;
	const { text, restore } = protectMath(body);
	let html = marked.parse(text, { async: false }) as string;
	html = restore(html);
	if (options.headingOffset) html = demoteHeadings(html, options.headingOffset);
	html = renderWikilinks(html, options.resolveNote);
	return DOMPurify.sanitize(html, {
		ADD_ATTR: ['target', 'rel'],
		// KaTeX emits MathML alongside its HTML output.
		USE_PROFILES: { html: true, mathMl: true, svg: true }
	});
}

/** Render note markdown for Read mode. Output is sanitized and safe to inject. */
export function renderNote(source: string, options: RenderOptions = {}): string {
	return renderMarkdown(source, { ...options, stripMeta: true });
}

function demoteHeadings(html: string, by: number): string {
	return html.replace(/<(\/?)h([1-6])\b/g, (_, slash, level) => {
		const next = Math.min(6, Number(level) + by);
		return `<${slash}h${next}`;
	});
}

/** Keep hand-made indentation visible in Read mode.
 *
 * Markdown folds an indented continuation line into the paragraph above it, so
 * outline-style notes lost their shape on the way to Read mode. Forcing
 * `white-space: pre-wrap` on every paragraph fixed that but broke the opposite
 * case: hard-wrapped prose then rendered with the source's line breaks instead
 * of reflowing to the reader's width.
 *
 * This preserves only the *leading* whitespace of lines markdown would
 * otherwise swallow, so indentation survives and ordinary paragraphs still
 * wrap. A genuine indented code block — 4+ columns after a blank line — is
 * left alone, since that indentation is already meaningful.
 */
export function preserveIndentation(source: string): string {
	let fenced = false;
	let previousBlank = true;
	let inIndentedCode = false;
	return source
		.split('\n')
		.map((line) => {
			if (/^\s*(```|~~~)/.test(line)) {
				fenced = !fenced;
				previousBlank = false;
				inIndentedCode = false;
				return line;
			}
			if (fenced) return line;
			if (!line.trim()) {
				// A blank line does not close an indented code block.
				previousBlank = true;
				return line;
			}

			const indent = /^[ \t]+(?=\S)/.exec(line)?.[0] ?? '';
			const width = indent.replace(/\t/g, '    ').length;
			const wasBlank = previousBlank;
			previousBlank = false;

			if (!indent) {
				inIndentedCode = false;
				return line;
			}
			// A code block opens at 4+ columns after a blank line and runs
			// through every indented line that follows.
			if (width >= 4 && (wasBlank || inIndentedCode)) {
				inIndentedCode = true;
				return line;
			}
			inIndentedCode = false;
			// Lists and quotes carry their own nesting already.
			if (/^[ \t]*([-*+]|\d+[.)]|>)\s/.test(line)) return line;
			return `<br>${'&nbsp;'.repeat(width)}${line.slice(indent.length)}`;
		})
		.join('\n');
}

function stripFrontmatter(source: string): string {
	if (!source.startsWith('---')) return source;
	const end = source.indexOf('\n---', 3);
	if (end === -1) return source;
	const after = source.indexOf('\n', end + 1);
	return after === -1 ? '' : source.slice(after + 1);
}

const WIKILINK_RE = /\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]/g;

/** `[[Note]]`, `[[Note#heading]]`, `[[Note|label]]` → links, with missing
 * targets shown as dead links rather than silently rendering as text. */
function renderWikilinks(html: string, resolve?: (name: string) => string | null): string {
	if (!resolve) return html;
	return html.replace(WIKILINK_RE, (whole, rawTarget, _heading, label) => {
		const target = String(rawTarget).trim();
		const text = escapeHtml(String(label ?? target).trim());
		const path = resolve(target);
		if (!path) return `<span class="dead-link" title="No note named ${escapeHtml(target)}">${text}</span>`;
		return `<a href="/note/${encodePath(path)}">${text}</a>`;
	});
}

function escapeHtml(value: string): string {
	return value
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;');
}

/** Headings for the outline panel, derived from the same source as Read mode. */
export interface Heading {
	level: number;
	text: string;
	line: number;
}

export function extractHeadings(source: string): Heading[] {
	const headings: Heading[] = [];
	let fenced = false;
	source.split('\n').forEach((raw, index) => {
		if (/^\s*(```|~~~)/.test(raw)) fenced = !fenced;
		if (fenced) return;
		const match = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(raw);
		if (match) {
			headings.push({
				level: match[1].length,
				// Strip the most common inline markup so the outline reads cleanly.
				text: match[2].replace(/[*_`]/g, '').trim(),
				line: index + 1
			});
		}
	});
	return headings;
}

export interface NoteStats {
	words: number;
	characters: number;
	readingMinutes: number;
}

export function noteStats(source: string): NoteStats {
	const body = stripFrontmatter(source);
	const words = body.split(/\s+/).filter(Boolean).length;
	return {
		words,
		characters: body.length,
		readingMinutes: Math.max(1, Math.round(words / 220))
	};
}
