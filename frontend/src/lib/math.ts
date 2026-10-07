// LaTeX rendering shared by the editor, Read mode, OMNIA's chat, and flashcards.
// KaTeX is bundled locally (fonts included) so the app keeps working offline and
// under a strict CSP.
import katex from 'katex';
// Imported here rather than in the root layout: the stylesheet is ~25 KB and
// registers ~20 @font-face rules, and it was being parsed on every route —
// including the login screen and the bare wordmark landing page, neither of
// which can contain math. Now it rides along only where math is rendered.
import 'katex/dist/katex.min.css';

/** Render a TeX string. Never throws — bad input falls back to its own source,
 * which is what you want mid-typing when the expression is half-written. */
export function renderTex(tex: string, display: boolean): string {
	try {
		return katex.renderToString(tex, {
			displayMode: display,
			throwOnError: false,
			output: 'html',
			strict: false
		});
	} catch {
		return escapeHtml(tex);
	}
}

function escapeHtml(value: string): string {
	return value
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;');
}

export interface MathSpan {
	from: number;
	to: number;
	tex: string;
	display: boolean;
}

// $$…$$ (display) or $…$ (inline). An inline span may not start or end on
// whitespace, which keeps prices like "$5 and $10" from being read as math.
const MATH_RE = /\$\$([\s\S]+?)\$\$|\$(?!\s)((?:[^$\n\\]|\\.)+?)(?<!\s)\$/g;

/** Locate every math span in a plain-text document. */
export function findMath(text: string): MathSpan[] {
	const spans: MathSpan[] = [];
	MATH_RE.lastIndex = 0;
	let match: RegExpExecArray | null;
	while ((match = MATH_RE.exec(text)) !== null) {
		const display = match[1] !== undefined;
		const tex = (display ? match[1] : match[2]).trim();
		if (!tex) continue;
		spans.push({ from: match.index, to: match.index + match[0].length, tex, display });
	}
	return spans;
}

/** Replace math spans in an HTML string with rendered KaTeX. Run this BEFORE
 * sanitizing so the sanitizer sees (and keeps) KaTeX's markup. */
export function renderMathInHtml(html: string): string {
	const spans = findMath(html);
	if (!spans.length) return html;
	let out = '';
	let cursor = 0;
	for (const span of spans) {
		out += html.slice(cursor, span.from);
		// The HTML escaper ran first, so unescape before handing TeX to KaTeX.
		const tex = span.tex
			.replace(/&lt;/g, '<')
			.replace(/&gt;/g, '>')
			.replace(/&quot;/g, '"')
			.replace(/&#39;/g, "'")
			.replace(/&amp;/g, '&');
		out += renderTex(tex, span.display);
		cursor = span.to;
	}
	return out + html.slice(cursor);
}
