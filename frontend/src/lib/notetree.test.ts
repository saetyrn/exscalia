// The tree the sidebar and the mobile /notes browser both draw. The rules
// that are easy to break silently — which roots are hidden, how each sort mode
// orders siblings — are the ones pinned down here.
import { describe, expect, it } from 'vitest';
import {
	buildTree,
	filterTree,
	isTreeNote,
	moveNoteInOrder,
	noteLabel,
	type TreeNode
} from './notetree';

/** Names at one level, folders rendered with a trailing slash. */
const shape = (nodes: TreeNode[]): string[] =>
	nodes.map((n) => (n.children === null ? n.name : `${n.name}/`));

const note = (path: string, extra: Partial<{ created: string; modified: string }> = {}) => ({
	path,
	...extra
});

describe('buildTree — nesting', () => {
	it('nests notes under the folders their paths name', () => {
		const tree = buildTree({
			notes: [note('root.md'), note('work/one.md'), note('work/deep/two.md')]
		});
		expect(shape(tree)).toEqual(['work/', 'root.md']);
		const work = tree[0];
		expect(shape(work.children!)).toEqual(['deep/', 'one.md']);
		expect(shape(work.children![0].children!)).toEqual(['two.md']);
		expect(work.children![0].path).toBe('work/deep');
	});

	it('keeps empty folders that only the folder list knows about', () => {
		const tree = buildTree({ notes: [note('a.md')], folders: ['archive'] });
		expect(shape(tree)).toEqual(['archive/', 'a.md']);
		expect(tree[0].children).toEqual([]);
	});

	it('puts folders before files at every level', () => {
		const tree = buildTree({ notes: [note('aaa.md'), note('zzz/inner.md')] });
		expect(shape(tree)).toEqual(['zzz/', 'aaa.md']);
	});
});

describe('buildTree — hidden roots', () => {
	it('drops the app-managed folders that have their own navigation', () => {
		const tree = buildTree({
			notes: [
				note('daily/2026-08-21.md'),
				note('omnia/log.md'),
				note('templates/meeting.md'),
				note('media/Dune.md'),
				note('recipes/carbonara.md'),
				note('recipes/weeknight/dal.md'),
				note('keep.md')
			],
			folders: ['daily', 'omnia', 'templates', 'media', 'recipes', 'recipes/weeknight', 'work']
		});
		expect(shape(tree)).toEqual(['work/', 'keep.md']);
	});

	it('drops the task inbox and every folder task board', () => {
		const tree = buildTree({
			notes: [
				note('Tasks Inbox.md'),
				{ path: 'work/Tasks.md', board: true },
				note('work/real.md')
			]
		});
		expect(shape(tree)).toEqual(['work/']);
		expect(shape(tree[0].children!)).toEqual(['real.md']);
	});

	it('agrees with isTreeNote about what it hides', () => {
		expect(isTreeNote({ path: 'daily/2026-08-21.md' })).toBe(false);
		expect(isTreeNote({ path: 'Tasks Inbox.md' })).toBe(false);
		expect(isTreeNote({ path: 'recipes/carbonara.md' })).toBe(false);
		expect(isTreeNote({ path: 'work/Tasks.md', board: true })).toBe(false);
		// A folder that merely starts with a hidden folder's name is fine.
		expect(isTreeNote({ path: 'dailyish/note.md' })).toBe(true);
	});
});

describe('buildTree — sort modes', () => {
	const notes = [
		note('b.md', { created: '2026-01-01', modified: '2026-08-03' }),
		note('a.md', { created: '2026-03-01', modified: '2026-08-01' }),
		note('c.md', { created: '2026-02-01', modified: '2026-08-02' })
	];

	it('sorts alphabetically by default', () => {
		expect(shape(buildTree({ notes }))).toEqual(['a.md', 'b.md', 'c.md']);
	});

	it('sorts newest first by creation date', () => {
		expect(shape(buildTree({ notes, sortMode: 'created' }))).toEqual(['a.md', 'c.md', 'b.md']);
	});

	it('sorts newest first by modification date', () => {
		expect(shape(buildTree({ notes, sortMode: 'modified' }))).toEqual(['b.md', 'c.md', 'a.md']);
	});

	it('sinks undated notes to the bottom instead of the top', () => {
		const tree = buildTree({
			notes: [note('undated.md'), note('dated.md', { modified: '2026-01-01' })],
			sortMode: 'modified'
		});
		expect(shape(tree)).toEqual(['dated.md', 'undated.md']);
	});

	it('follows the saved arrangement in manual mode, unknowns last', () => {
		const tree = buildTree({
			notes: [...notes, note('d.md')],
			sortMode: 'manual',
			noteOrder: ['c.md', 'a.md', 'b.md']
		});
		expect(shape(tree)).toEqual(['c.md', 'a.md', 'b.md', 'd.md']);
	});

	it('follows the saved folder arrangement regardless of note sort', () => {
		const tree = buildTree({
			notes: [note('zoo/x.md'), note('alpha/y.md'), note('mid/z.md')],
			folderOrder: ['zoo', 'mid']
		});
		expect(shape(tree)).toEqual(['zoo/', 'mid/', 'alpha/']);
	});
});

describe('moveNoteInOrder', () => {
	// Saved arrangement and the tree as manual mode draws it: work/ first
	// (folders lead), then the root notes; new.md is not arranged yet, so it
	// trails its folder alphabetically.
	const order = ['work/b.md', 'work/c.md', 'work/a.md', 'z.md', 'y.md'];
	const displayed = ['work/b.md', 'work/c.md', 'work/a.md', 'work/new.md', 'z.md', 'y.md'];

	it('moves a note to the first slot of its folder', () => {
		expect(moveNoteInOrder(order, displayed, 'work/a.md', null, 'work')).toEqual([
			'work/a.md',
			'work/b.md',
			'work/c.md',
			'z.md',
			'y.md',
			'work/new.md'
		]);
	});

	it('moves a note to the end of its folder', () => {
		const next = moveNoteInOrder(order, displayed, 'work/b.md', 'work/new.md', 'work');
		expect(next.filter((p) => p.startsWith('work/'))).toEqual([
			'work/c.md',
			'work/a.md',
			'work/new.md',
			'work/b.md'
		]);
	});

	it('leaves the other folders in the order they had', () => {
		const next = moveNoteInOrder(order, displayed, 'y.md', null, '');
		expect(next.filter((p) => p.startsWith('work/'))).toEqual([
			'work/b.md',
			'work/c.md',
			'work/a.md',
			'work/new.md'
		]);
		expect(next.filter((p) => !p.includes('/'))).toEqual(['y.md', 'z.md']);
	});

	it('keeps the saved arrangement under a different displayed sort', () => {
		// The tree drawn alphabetically must not leak into what is saved.
		const alphabetical = ['work/a.md', 'work/b.md', 'work/c.md', 'work/new.md', 'y.md', 'z.md'];
		const next = moveNoteInOrder(order, alphabetical, 'z.md', 'y.md', '');
		expect(next.filter((p) => p.startsWith('work/'))).toEqual([
			'work/b.md',
			'work/c.md',
			'work/a.md',
			'work/new.md'
		]);
		expect(next.filter((p) => !p.includes('/'))).toEqual(['y.md', 'z.md']);
	});

	it('places notes the arrangement has not seen in display order', () => {
		const shown = ['work/p.md', 'work/q.md', 'z.md', 'y.md'];
		const next = moveNoteInOrder(['z.md'], shown, 'z.md', 'y.md', '');
		expect(next).toEqual(['work/p.md', 'work/q.md', 'y.md', 'z.md']);
	});

	it('moves a note that is not in the saved order yet', () => {
		const next = moveNoteInOrder(order, displayed, 'work/new.md', 'work/b.md', 'work');
		expect(next.filter((p) => p.startsWith('work/'))).toEqual([
			'work/b.md',
			'work/new.md',
			'work/c.md',
			'work/a.md'
		]);
		expect(next.filter((p) => p === 'work/new.md')).toHaveLength(1);
	});

	it('drops saved entries the tree no longer shows', () => {
		const next = moveNoteInOrder(['gone.md', ...order], displayed, 'z.md', null, '');
		expect(next).not.toContain('gone.md');
	});

	it('appends when the folder has no other notes', () => {
		expect(moveNoteInOrder(['a.md'], ['solo/x.md', 'a.md'], 'solo/x.md', null, 'solo')).toEqual([
			'a.md',
			'solo/x.md'
		]);
	});
});

describe('filterTree', () => {
	const tree = buildTree({
		notes: [note('work/report.md'), note('work/notes.md'), note('personal/diary.md')]
	});

	it('keeps folders that still have a surviving note', () => {
		const filtered = filterTree(tree, (n) => n.children !== null || n.name.includes('report'));
		expect(shape(filtered)).toEqual(['personal/', 'work/']);
		expect(shape(filtered.find((n) => n.name === 'work')!.children!)).toEqual(['report.md']);
		// personal survives on its own name, but with nothing under it.
		expect(filtered.find((n) => n.name === 'personal')!.children).toEqual([]);
	});

	it('drops folders that match nothing at all', () => {
		const filtered = filterTree(tree, (n) => n.children === null && n.name.includes('diary'));
		expect(shape(filtered)).toEqual(['personal/']);
	});
});

describe('noteLabel', () => {
	it('strips only the markdown extension', () => {
		expect(noteLabel('Plan.md')).toBe('Plan');
		expect(noteLabel('notes.md.md')).toBe('notes.md');
		expect(noteLabel('README')).toBe('README');
	});
});
