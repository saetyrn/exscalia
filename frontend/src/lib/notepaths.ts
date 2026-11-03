/** First free `<stem>.md` / `<stem> N.md` path inside `folder`.
 *
 * One home for the numbering every create path uses — the sidebar, the
 * command palette, and template instantiation must agree on what "next
 * untitled" means or they collide with each other's notes. */
export function numberedPath(
	notes: { path: string }[],
	stem: string,
	folder = ''
): string {
	const prefix = folder ? `${folder}/` : '';
	const existing = new Set(notes.map((note) => note.path.toLowerCase()));
	let number = 1;
	let path = `${prefix}${stem}.md`;
	while (existing.has(path.toLowerCase())) {
		number += 1;
		path = `${prefix}${stem} ${number}.md`;
	}
	return path;
}
