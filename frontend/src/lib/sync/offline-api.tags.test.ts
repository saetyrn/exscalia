// Offline, the desktop app derives the tag list and tag pages from its cache.
// That has to use the grammar the server indexes with, or losing the
// connection changes which tags exist (#2026, a #tag inside a code fence).
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { NoteRow } from './db';

const cache = vi.hoisted(() => ({ rows: [] as NoteRow[] }));

vi.mock('./db', () => ({ allNotes: async () => cache.rows }));
vi.mock('./engine', () => ({
	isOffline: () => true,
	computeRevision: async () => '',
	refreshPendingBadge: async () => {},
	pushPath: async () => {},
	schedulePush: () => {}
}));

const { api } = await import('../api');
const { installOfflineApi } = await import('./offline-api');
installOfflineApi();

function row(path: string, content: string, meta: Record<string, unknown> = {}): NoteRow {
	return {
		path,
		name: path.replace(/\.md$/, ''),
		pinned: 0,
		content,
		revision: null,
		base_content: content,
		base_revision: null,
		meta_json: JSON.stringify(meta),
		sync_status: 'synced',
		fetched_at: null,
		updated_at: null
	};
}

beforeEach(() => {
	cache.rows = [
		row('a.md', '---\ntags: [work]\n---\n\n#café #2026 #Übung\n```\n#fenced\n```\n', {
			tags: ['work']
		}),
		row('b.md', 'decomposed #café')
	];
});

describe('offline tags', () => {
	it('counts only what the server would index', async () => {
		expect(await api.listTags()).toEqual([
			{ tag: 'café', count: 2 },
			{ tag: 'work', count: 1 }
		]);
	});

	it('lists a note under its tag and nothing under a fenced one', async () => {
		expect((await api.notesForTag('café')).map((n) => n.path)).toEqual(['a.md', 'b.md']);
		expect(await api.notesForTag('fenced')).toEqual([]);
	});
});
