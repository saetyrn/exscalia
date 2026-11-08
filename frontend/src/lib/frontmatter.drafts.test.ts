// Managed stamps are rewritten by the server on every save, so the client's
// copy lags them by design. Anything that asks "is this text already saved?"
// (draft recovery on open, fe-editor#7) has to look past them.
import { describe, expect, it } from 'vitest';
import { sameExceptManagedStamps } from './frontmatter';

const note = (modified: string, body: string, tags = 'plan') =>
	['---', 'created: 2026-09-01T10:00:00', `modified: ${modified}`, `tags: [${tags}]`, '---', body].join(
		'\n'
	);

describe('sameExceptManagedStamps', () => {
	it('treats a draft that differs from the server only by the save stamp as saved', () => {
		const draft = note('2026-09-22T10:00:00', '# Plan\n\nShip v2.\n');
		const server = note('2026-09-22T10:00:02', '# Plan\n\nShip v2.\n');
		expect(sameExceptManagedStamps(draft, server)).toBe(true);
	});

	it('still sees an unsaved body edit', () => {
		const draft = note('2026-09-22T10:00:00', '# Plan\n\nShip v2. Write docs.\n');
		const server = note('2026-09-22T10:00:02', '# Plan\n\nShip v2.\n');
		expect(sameExceptManagedStamps(draft, server)).toBe(false);
	});

	it('still sees an unsaved frontmatter edit the user made', () => {
		const draft = note('2026-09-22T10:00:00', 'body\n', 'plan, launch');
		const server = note('2026-09-22T10:00:02', 'body\n');
		expect(sameExceptManagedStamps(draft, server)).toBe(false);
	});

	it('compares notes without frontmatter byte for byte', () => {
		expect(sameExceptManagedStamps('plain\n', 'plain\n')).toBe(true);
		expect(sameExceptManagedStamps('plain\n', 'plain \n')).toBe(false);
	});
});
