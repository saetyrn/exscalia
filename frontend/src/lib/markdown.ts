import { renderMarkdown } from './render';

/** The `tags:` entry of a raw frontmatter block, without YAML machinery.
 * Handles the three shapes notes actually contain: `tags: [a, b]`,
 * a dash list, and a bare scalar. */
export function parseFrontmatterTags(content: string): string[] {
	if (!content.startsWith('---')) return [];
	const end = content.indexOf('\n---', 3);
	if (end === -1) return [];
	const lines = content.slice(0, end).split('\n');
	const tags: string[] = [];
	const push = (raw: string) => {
		const tag = raw.trim().replace(/^['"]|['"]$/g, '').replace(/^#/, '');
		if (tag && !tags.includes(tag)) tags.push(tag);
	};
	for (let i = 0; i < lines.length; i++) {
		const m = /^tags:\s*(.*)$/.exec(lines[i]);
		if (!m) continue;
		const rest = m[1].trim();
		if (rest.startsWith('[')) {
			for (const part of rest.replace(/^\[|\]$/g, '').split(',')) push(part);
		} else if (rest) {
			push(rest);
		} else {
			// Dash items may be indented or not — PyYAML emits them at column 0.
			for (let j = i + 1; j < lines.length; j++) {
				const item = /^\s*-\s*(.+)$/.exec(lines[j]);
				if (!item) break;
				push(item[1]);
			}
		}
		break;
	}
	return tags;
}

/** Tags as the editor's live document has them — frontmatter parsed from the
 * text itself (server meta may lag unsaved edits) plus inline #hashtags. */
export function extractLiveTags(content: string): string[] {
	return extractNoteTags(content, { tags: parseFrontmatterTags(content) });
}

/** Return `content` with `tag` added to the frontmatter tags entry, creating
 * the entry (or the whole block) when missing. Returns content unchanged if
 * the tag is already present. */
export function addFrontmatterTag(content: string, tag: string): string {
	if (extractLiveTags(content).includes(tag)) return content;
	if (!content.startsWith('---') || content.indexOf('\n---', 3) === -1) {
		return `---\ntags: [${tag}]\n---\n\n${content}`;
	}
	const end = content.indexOf('\n---', 3);
	const head = content.slice(0, end);
	const tail = content.slice(end);
	const lines = head.split('\n');
	for (let i = 0; i < lines.length; i++) {
		const m = /^tags:\s*(.*)$/.exec(lines[i]);
		if (!m) continue;
		const rest = m[1].trim();
		if (rest.startsWith('[')) {
			lines[i] = rest === '[]' ? `tags: [${tag}]` : lines[i].replace(/\]\s*$/, `, ${tag}]`);
		} else if (rest) {
			lines[i] = `tags: [${rest.replace(/^['"]|['"]$/g, '')}, ${tag}]`;
		} else {
			// Dash list: append after the last item, matching its indentation
			// (the server emits dashes at column 0). A bare `tags:` with no
			// items becomes the inline form instead of guessing an indent.
			let last = i;
			for (let j = i + 1; j < lines.length && /^\s*-\s*\S/.test(lines[j]); j++) last = j;
			if (last === i) {
				lines[i] = `tags: [${tag}]`;
			} else {
				const indent = /^\s*/.exec(lines[last])?.[0] ?? '';
				lines.splice(last + 1, 0, `${indent}- ${tag}`);
			}
		}
		return lines.join('\n') + tail;
	}
	// No tags entry: add one just before the closing fence.
	return `${head}\ntags: [${tag}]${tail}`;
}

/** Return `content` with `tag` removed from the frontmatter tags entry.
 * Inline body #hashtags are the user's prose and are left alone. Removing
 * the last tag drops the whole entry. */
export function removeFrontmatterTag(content: string, tag: string): string {
	if (!content.startsWith('---')) return content;
	const end = content.indexOf('\n---', 3);
	if (end === -1) return content;
	const head = content.slice(0, end);
	const tail = content.slice(end);
	const lines = head.split('\n');
	const clean = (raw: string) => raw.trim().replace(/^['"]|['"]$/g, '').replace(/^#/, '');
	for (let i = 0; i < lines.length; i++) {
		const m = /^tags:\s*(.*)$/.exec(lines[i]);
		if (!m) continue;
		const rest = m[1].trim();
		if (rest.startsWith('[')) {
			const kept = rest
				.replace(/^\[|\]$/g, '')
				.split(',')
				.map((part) => part.trim())
				.filter((part) => part && clean(part) !== tag);
			if (kept.length) lines[i] = `tags: [${kept.join(', ')}]`;
			else lines.splice(i, 1);
		} else if (rest) {
			if (clean(rest) !== tag) return content;
			lines.splice(i, 1);
		} else {
			let removed = false;
			let items = 0;
			for (let j = i + 1; j < lines.length; ) {
				const item = /^\s*-\s*(.+)$/.exec(lines[j]);
				if (!item) break;
				if (clean(item[1]) === tag) {
					lines.splice(j, 1);
					removed = true;
				} else {
					items += 1;
					j += 1;
				}
			}
			if (!removed) return content;
			if (items === 0) lines.splice(i, 1); // no items left — drop `tags:`
		}
		return lines.join('\n') + tail;
	}
	return content;
}

/**
 * Tags for a note: inline #hashtags in the body (primary), plus any legacy
 * frontmatter tags:. Mirrors the backend indexer's extraction.
 */
export function extractNoteTags(content: string, meta: Record<string, unknown>): string[] {
	const tags: string[] = [];
	const fmTags = meta.tags;
	if (Array.isArray(fmTags)) {
		for (const t of fmTags) {
			const s = String(t).trim().replace(/^#/, '');
			if (s && !tags.includes(s)) tags.push(s);
		}
	}
	let body = content;
	if (body.startsWith('---\n')) {
		const end = body.indexOf('\n---', 3);
		if (end !== -1) body = body.slice(end + 4);
	}
	body = body.replace(/^(```|~~~)[\s\S]*?^\1[^\n]*$/gm, '');
	for (const m of body.matchAll(/(?<![\w#])#([A-Za-z][\w/-]*)/g)) {
		if (!tags.includes(m[1])) tags.push(m[1]);
	}
	return tags;
}

/**
 * Render an OMNIA chat reply. Uses the same sanitized markdown pipeline as
 * Read mode, so tables, fenced code, numbered lists, links, and blockquotes
 * all render — the briefing tells OMNIA it can use them, and previously they
 * came out as literal text.
 */
export function renderChatMarkdown(src: string, noteExists: (name: string) => string | null): string {
	return renderMarkdown(src, { resolveNote: noteExists, headingOffset: 3 });
}

