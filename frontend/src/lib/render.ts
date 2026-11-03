// Read mode: full markdown → sanitized HTML. This is the only place notes are
// rendered as a finished document rather than as source.
import DOMPurify from 'dompurify';
import { Marked } from 'marked';
import { renderMathInHtml } from './math';
import { noteHref } from './recipes/links';

const marked = new Marked({ gfm: true, breaks: false });

/** The options of the render in progress. The extension and renderer
 * overrides below are registered once, but the wikilink resolver and chat
 * mode vary per call; `marked.parse` is synchronous, so parseMarkdown sets
 * this around it. */
let active: RenderOptions | null = null;

/** `[[Note]]`, `[[Note#heading]]`, `[[Note|label]]`, matched on the source so
 * the target is the name as written, not marked's escaped HTML of it. */
const WIKILINK = /^\[\[([^\]|#\n]+)(?:#([^\]|\n]*))?(?:\|([^\]\n]*))?\]\]/;

marked.use({
	extensions: [
		{
			name: 'wikilink',
			level: 'inline',
			// Without a resolver (flashcards, media reviews) `[[…]]` stays the
			// plain text marked would make of it anyway.
			start: (src) => (active?.resolveNote ? src.indexOf('[[') : undefined),
			tokenizer(src) {
				if (!active?.resolveNote) return undefined;
				const match = WIKILINK.exec(src);
				const target = match?.[1].trim();
				if (!match || !target) return undefined;
				return { type: 'wikilink', raw: match[0], target, label: match[3]?.trim() || target };
			},
			// Missing targets show as dead links rather than silently as text.
			renderer(token) {
				const label = escapeHtml(token.label);
				const path = active?.resolveNote?.(token.target);
				if (!path) return `<span class="dead-link" title="No note named ${escapeHtml(token.target)}">${label}</span>`;
				return `<a href="${noteHref(path)}">${label}</a>`;
			}
		}
	],
	renderer: {
		// A chat reply is model output, and the model reads text it didn't
		// write. An image loads the moment the reply renders, so a remote src
		// is a request carrying whatever the model put in the URL. In chat
		// those become a link the user has to follow.
		image({ href, text, tokens }) {
			if (!active?.chat || isLocalImage(href)) return false;
			const alt = tokens ? this.parser.parseInline(tokens, this.parser.textRenderer) : text;
			return `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(alt || href)}</a>`;
		},
		// Raw HTML can fetch in more ways than <img> (srcset, poster, style
		// url(), svg <image>), and the agent is never asked for HTML, so chat
		// shows it as text. `<br>` is the exception: replies use it for line
		// breaks inside table cells, and a bare one loads nothing.
		html({ text }) {
			if (!active?.chat || /^<br\s*\/?>$/i.test(text)) return false;
			return escapeHtml(text);
		}
	},
	hooks: {
		// Keeps a `*` or `_` inside a wikilink from pairing with emphasis
		// around it. The mask has to keep the source's length.
		emStrongMask(src) {
			if (!active?.resolveNote) return src;
			return src.replace(/\[\[[^\]\n]+\]\]/g, (link) => `[${'a'.repeat(link.length - 2)}]`);
		}
	}
});

/** Images a chat reply may load without a click: the vault's attachments and
 * Paper renders, and inline data. Dot segments are resolved the way the
 * browser will, so `/api/attachments/../…` cannot step outside the prefix. */
function isLocalImage(href: string): boolean {
	const src = href.trim();
	if (/^(data|blob):/i.test(src)) return true;
	if (!/^\/?api\/(attachments|paper)\//.test(src)) return false;
	try {
		const url = new URL(src, 'http://self.invalid/');
		return url.host === 'self.invalid' && /^\/api\/(attachments|paper)\//.test(url.pathname);
	} catch {
		return false;
	}
}

const EXTERNAL_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);

/** Mark an off-site link to open outside the app.
 *
 * A plain click on an external anchor navigates the current page. On the web
 * that loses the app; in the desktop shell it replaces EXSCALIA in its only
 * window with no back control. `target="_blank"` makes the browser open a new
 * tab instead, and it is also the one signal the Tauri opener plugin's click
 * script acts on to hand the URL to the system browser. Internal links
 * (`/note/…` wikilinks, `#fragments`, same-origin URLs) are left alone so the
 * router keeps them. */
export function decorateExternalAnchor(node: {
	tagName: string;
	getAttribute(name: string): string | null;
	setAttribute(name: string, value: string): void;
}): void {
	if (node.tagName !== 'A') return;
	const href = node.getAttribute('href');
	if (!href) return;
	let url: URL;
	try {
		url = typeof location === 'undefined' ? new URL(href) : new URL(href, location.href);
	} catch {
		return;
	}
	// Protocol first: a relative href under the desktop's tauri:// origin
	// resolves to a non-special scheme whose URL origin is opaque.
	if (!EXTERNAL_PROTOCOLS.has(url.protocol)) return;
	if (typeof location !== 'undefined' && url.origin === location.origin) return;
	node.setAttribute('target', '_blank');
	node.setAttribute('rel', 'noopener noreferrer');
}

// The default DOMPurify instance is only used here, so the hook applies to
// every render and nowhere else. Without a DOM (the unit tests run under
// node) DOMPurify is an inert stub with no addHook, and nothing renders.
if (DOMPurify.isSupported) DOMPurify.addHook('afterSanitizeAttributes', decorateExternalAnchor);

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
	/** OMNIA's replies: remote images become links and raw HTML shows as text. */
	chat?: boolean;
	/** Keep the result for the next render of the same source (default). A
	 * reply still streaming passes false: every partial text would be an entry
	 * of its own, and a long one evicted every finished message it scrolled past. */
	cache?: boolean;
}

/** Finished HTML, keyed on what produced it.
 *
 * OMNIA's chat re-renders the whole transcript on every streamed token, so a
 * twenty-message thread was parsing twenty replies (marked + KaTeX +
 * DOMPurify) per token — quadratic work for output that never changes once a
 * message is complete. Rendering is pure for a given source and options, so
 * the result is kept.
 *
 * Wikilinks are the one impure part: `[[Note]]` resolves against the live note
 * list, and a note created later must stop reading as a dead link. Sources
 * containing `[[` therefore skip the cache rather than freezing that answer.
 */
const RENDER_CACHE_LIMIT = 200;
const renderCache = new Map<string, string>();

function cachedRender(key: string, produce: () => string): string {
	const hit = renderCache.get(key);
	if (hit !== undefined) {
		// Re-insert so this counts as the newest entry. A Map iterates in
		// insertion order, which is what makes the oldest-first eviction below
		// least-recently-used rather than first-in-first-out.
		renderCache.delete(key);
		renderCache.set(key, hit);
		return hit;
	}
	const html = produce();
	renderCache.set(key, html);
	if (renderCache.size > RENDER_CACHE_LIMIT) {
		const oldest = renderCache.keys().next().value;
		if (oldest !== undefined) renderCache.delete(oldest);
	}
	return html;
}

/** Markdown → sanitized HTML. Shared by Read mode and OMNIA's chat so both
 * support the same syntax: tables, fenced code, lists, links, quotes, math. */
export function renderMarkdown(source: string, options: RenderOptions = {}): string {
	if (options.cache === false || source.includes('[[')) return parseMarkdown(source, options);
	// resolveNote only ever touches `[[…]]`, so it cannot affect this output
	// and stays out of the key; the options that can are in it (`cache` only
	// says whether to look, not what comes out).
	return cachedRender(
		`${options.headingOffset ?? 0}:${options.stripMeta ? 1 : 0}:${options.chat ? 1 : 0}:${source}`,
		() => parseMarkdown(source, options)
	);
}

function parseMarkdown(source: string, options: RenderOptions): string {
	const body = options.stripMeta ? preserveIndentation(stripFrontmatter(source)) : source;
	const { text, restore } = protectMath(body);
	const previous = active;
	active = options;
	let html: string;
	try {
		html = marked.parse(text, { async: false }) as string;
	} finally {
		active = previous;
	}
	html = restore(html);
	if (options.headingOffset) html = demoteHeadings(html, options.headingOffset);
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
