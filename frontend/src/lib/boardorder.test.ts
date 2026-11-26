// The task board's ordering. The rules pinned down here are the ones a drag
// exercises without anyone looking: where the preview puts the card, what id
// list a drop or a Move up/down sends, and that a reorder can never write a
// card into a column it is not in.
import { describe, expect, it } from 'vitest';
import {
	applyOrder,
	orderAfterPlace,
	orderAfterStep,
	previewColumn,
	type BoardCard
} from './boardorder';

const card = (id: string, status: BoardCard['status']): BoardCard => ({ id, status });

// Interleaved on purpose: the columns share one array, so a reorder that
// strays outside its own slots shows up as a moved foreign card.
const board = (): BoardCard[] => [
	card('T1', 'todo'),
	card('D1', 'in_progress'),
	card('T2', 'todo'),
	card('X', 'done'),
	card('D2', 'in_progress')
];

const ids = (cards: BoardCard[]) => cards.map((c) => c.id);

describe('previewColumn', () => {
	it('is the plain column when nothing is being dragged', () => {
		expect(ids(previewColumn(board(), 'todo', null, null))).toEqual(['T1', 'T2']);
		expect(ids(previewColumn(board(), 'todo', 'T1', null))).toEqual(['T1', 'T2']);
	});

	it('lifts the card out of its column and into the aimed gap of another', () => {
		const tasks = board();
		const aim = { status: 'in_progress' as const, index: 1 };
		expect(ids(previewColumn(tasks, 'todo', 'T1', aim))).toEqual(['T2']);
		expect(ids(previewColumn(tasks, 'in_progress', 'T1', aim))).toEqual(['D1', 'T1', 'D2']);
	});

	it('leaves a column the drag is neither leaving nor aiming at untouched', () => {
		const aim = { status: 'in_progress' as const, index: 0 };
		expect(ids(previewColumn(board(), 'done', 'T1', aim))).toEqual(['X']);
	});

	it('counts gaps without the dragged card within its own column', () => {
		// T1 over gap 1 of [T2] lands after T2, not back in its own slot.
		const aim = { status: 'todo' as const, index: 1 };
		expect(ids(previewColumn(board(), 'todo', 'T1', aim))).toEqual(['T2', 'T1']);
	});

	it('clamps a gap past the end to the end', () => {
		const aim = { status: 'in_progress' as const, index: 9 };
		expect(ids(previewColumn(board(), 'in_progress', 'T1', aim))).toEqual(['D1', 'D2', 'T1']);
	});
});

describe('orderAfterPlace', () => {
	// T1 already moved into the column, as it is once the status change lands.
	const moved = (): BoardCard[] => board().map((c) => (c.id === 'T1' ? card('T1', 'in_progress') : c));

	it('puts the card at the top for gap 0', () => {
		expect(orderAfterPlace(moved(), 'T1', 'in_progress', 0)).toEqual(['T1', 'D1', 'D2']);
	});

	it('puts the card between its neighbours for a middle gap', () => {
		expect(orderAfterPlace(moved(), 'T1', 'in_progress', 1)).toEqual(['D1', 'T1', 'D2']);
	});

	it('clamps a gap past the end to the end', () => {
		expect(orderAfterPlace(moved(), 'T1', 'in_progress', 7)).toEqual(['D1', 'D2', 'T1']);
	});

	it('reads the gap among the column without the card when reordering in place', () => {
		// [T1, T2]: gap 1 among [T2] is after T2.
		expect(orderAfterPlace(board(), 'T1', 'todo', 1)).toEqual(['T2', 'T1']);
		expect(orderAfterPlace(board(), 'T1', 'todo', 0)).toEqual(['T1', 'T2']);
	});
});

describe('orderAfterStep', () => {
	it('returns null at either end of the column', () => {
		expect(orderAfterStep(board(), 'T1', -1)).toBeNull();
		expect(orderAfterStep(board(), 'T2', 1)).toBeNull();
		expect(orderAfterStep(board(), 'X', 1)).toBeNull();
	});

	it('returns null for an unknown card', () => {
		expect(orderAfterStep(board(), 'nope', 1)).toBeNull();
	});

	it('swaps with the neighbour in the middle of the column', () => {
		const tasks = [...board(), card('D3', 'in_progress')];
		expect(orderAfterStep(tasks, 'D2', -1)).toEqual(['D2', 'D1', 'D3']);
		expect(orderAfterStep(tasks, 'D2', 1)).toEqual(['D1', 'D3', 'D2']);
	});
});

describe('applyOrder', () => {
	it('reorders only the target column, in its own slots', () => {
		const tasks = board();
		const next = applyOrder(tasks, 'in_progress', ['D2', 'D1']);
		expect(ids(next)).toEqual(['T1', 'D2', 'T2', 'X', 'D1']);
		// The other columns keep the very same objects at the very same indices.
		for (const i of [0, 2, 3]) expect(next[i]).toBe(tasks[i]);
	});

	it('returns a new array and leaves the input alone', () => {
		const tasks = board();
		const next = applyOrder(tasks, 'todo', ['T2', 'T1']);
		expect(next).not.toBe(tasks);
		expect(ids(tasks)).toEqual(['T1', 'D1', 'T2', 'X', 'D2']);
	});

	it('is a no-op for more ids than the column has slots', () => {
		const tasks = board();
		expect(applyOrder(tasks, 'in_progress', ['D1', 'D2', 'X'])).toBe(tasks);
	});

	it('is a no-op for a foreign id, a missing id, or a repeated id', () => {
		const tasks = board();
		expect(applyOrder(tasks, 'in_progress', ['D1', 'X'])).toBe(tasks);
		expect(applyOrder(tasks, 'in_progress', ['D1'])).toBe(tasks);
		expect(applyOrder(tasks, 'in_progress', ['D1', 'D1'])).toBe(tasks);
	});

	it('refuses the order a failed cross-column drop would build', () => {
		// The status change to In progress failed and rolled back: T1 is still
		// a To do card when the drop's placement runs.
		const tasks = board();
		const next = orderAfterPlace(tasks, 'T1', 'in_progress', 0);
		expect(next).toEqual(['T1', 'D1', 'D2']);
		// Applying it would write T1 into an In progress slot (twice on the
		// board) and drop D2. The board must stay exactly as it was.
		expect(applyOrder(tasks, 'in_progress', next)).toBe(tasks);
	});
});
