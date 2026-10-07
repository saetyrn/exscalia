// Frontmatter the app maintains, as opposed to what the user writes.
//
// `created` and `modified` are stamped by the server on every save, so the
// response always differs from what the editor sent — even when the user's
// text is byte-identical. Treating that as a real change made the editor
// replace its document after every autosave, which blurs the contenteditable
// (focus lands on <body>) and drops any keystroke that arrives in the gap.

const MANAGED_KEYS = ['created', 'modified'] as const;

interface Split {
	frontmatter: string | null;
	body: string;
}

/** Split a note into its frontmatter block and everything after it. */
export function splitFrontmatter(text: string): Split {
	if (!text.startsWith('---')) return { frontmatter: null, body: text };
	const end = text.indexOf('\n---', 3);
	if (end === -1) return { frontmatter: null, body: text };
	const after = text.indexOf('\n', end + 1);
	if (after === -1) return { frontmatter: text, body: '' };
	return { frontmatter: text.slice(0, after + 1), body: text.slice(after + 1) };
}

function stampsRemoved(frontmatter: string | null): string {
	if (frontmatter === null) return '';
	return frontmatter
		.split('\n')
		.filter((line) => !MANAGED_KEYS.some((key) => line.startsWith(`${key}:`)))
		.join('\n');
}

/**
 * True when two versions differ only by the timestamps the app maintains.
 *
 * The editor has no reason to redraw for those: they are hidden in Formatted
 * mode, and rewriting the document to show them costs the user their focus.
 */
export function sameExceptManagedStamps(a: string, b: string): boolean {
	if (a === b) return true;
	const left = splitFrontmatter(a);
	const right = splitFrontmatter(b);
	if (left.body !== right.body) return false;
	return stampsRemoved(left.frontmatter) === stampsRemoved(right.frontmatter);
}

/**
 * Give `text` the managed timestamps that `reference` carries.
 *
 * Used before a three-way merge: once the editor stops redrawing for a stamp
 * change, its copy can lag the server's by one save. Without this, that lag
 * shows up as an edit to the same frontmatter line the other device just
 * touched — a conflict over a value neither person typed.
 */
export function alignManagedStamps(text: string, reference: string): string {
	const target = splitFrontmatter(text);
	const source = splitFrontmatter(reference);
	if (target.frontmatter === null || source.frontmatter === null) return text;

	const wanted = new Map<string, string>();
	for (const line of source.frontmatter.split('\n')) {
		const key = MANAGED_KEYS.find((candidate) => line.startsWith(`${candidate}:`));
		if (key) wanted.set(key, line);
	}
	if (wanted.size === 0) return text;

	const rewritten = target.frontmatter
		.split('\n')
		.map((line) => {
			const key = MANAGED_KEYS.find((candidate) => line.startsWith(`${candidate}:`));
			return key && wanted.has(key) ? wanted.get(key)! : line;
		})
		.join('\n');
	return rewritten + target.body;
}
