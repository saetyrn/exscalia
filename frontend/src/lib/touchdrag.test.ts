// Touch drag edge autoscroll. A live drag blocks native scrolling, so without
// this a folder or card slot below the fold could not be reached by dragging.
// The runner has no DOM: the gesture tests drive the action with plain
// EventTargets and just enough element surface for what touchdrag.ts touches.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { edgeScrollSpeed, scrollParent, touchDrag } from './touchdrag';

const RECT = { top: 100, bottom: 700 };

describe('edgeScrollSpeed', () => {
	it('is 0 away from the edges', () => {
		expect(edgeScrollSpeed(400, RECT, 48, 14)).toBe(0);
		expect(edgeScrollSpeed(148, RECT, 48, 14)).toBe(0);
		expect(edgeScrollSpeed(652, RECT, 48, 14)).toBe(0);
	});

	it('scrolls up near the top and down near the bottom', () => {
		expect(edgeScrollSpeed(130, RECT, 48, 14)).toBeLessThan(0);
		expect(edgeScrollSpeed(670, RECT, 48, 14)).toBeGreaterThan(0);
	});

	it('speeds up toward the edge and caps at max', () => {
		const up = [140, 130, 115].map((y) => -edgeScrollSpeed(y, RECT, 48, 14));
		const down = [660, 670, 685].map((y) => edgeScrollSpeed(y, RECT, 48, 14));
		for (const speeds of [up, down]) {
			expect(speeds[0]).toBeGreaterThan(0);
			expect(speeds[1]).toBeGreaterThan(speeds[0]);
			expect(speeds[2]).toBeGreaterThan(speeds[1]);
			expect(speeds[2]).toBeLessThanOrEqual(14);
		}
		expect(edgeScrollSpeed(100, RECT, 48, 14)).toBe(-14);
		expect(edgeScrollSpeed(700, RECT, 48, 14)).toBe(14);
		// Past the edge (over the search field, the tab bar) stays at max.
		expect(edgeScrollSpeed(-80, RECT, 48, 14)).toBe(-14);
		expect(edgeScrollSpeed(900, RECT, 48, 14)).toBe(14);
	});

	it('keeps the zones apart on a short scroller', () => {
		const short = { top: 0, bottom: 60 };
		expect(edgeScrollSpeed(30, short)).toBe(0);
		expect(edgeScrollSpeed(10, short)).toBeLessThan(0);
		expect(edgeScrollSpeed(50, short)).toBeGreaterThan(0);
		expect(edgeScrollSpeed(0, { top: 0, bottom: 0 })).toBe(0);
	});
});

interface FakeBox {
	parentElement: FakeBox | null;
	overflowY: string;
	scrollHeight: number;
	clientHeight: number;
}

function box(overflowY: string, scrollHeight: number, clientHeight: number, parent: FakeBox | null) {
	return { parentElement: parent, overflowY, scrollHeight, clientHeight } as FakeBox;
}

const asEl = (b: FakeBox) => b as unknown as Element;

describe('scrollParent', () => {
	const page = { page: true };
	beforeEach(() => {
		vi.stubGlobal('document', { scrollingElement: page });
		vi.stubGlobal('getComputedStyle', (el: FakeBox) => ({ overflowY: el.overflowY }));
	});
	afterEach(() => vi.unstubAllGlobals());

	it('finds the nearest ancestor that scrolls', () => {
		// The mobile drawer: nav grows (overflow visible), the rail scrolls.
		const rail = box('auto', 2000, 600, null);
		const nav = box('visible', 1900, 1900, rail);
		const row = box('visible', 40, 40, nav);
		expect(scrollParent(asEl(row))).toBe(rail);
	});

	it('skips a scroll container with nothing to scroll', () => {
		const outer = box('scroll', 3000, 800, null);
		const nav = box('auto', 300, 300, outer);
		const row = box('visible', 40, 40, nav);
		expect(scrollParent(asEl(row))).toBe(outer);
	});

	it('does not count the element itself, and falls back to the page', () => {
		const wrap = box('hidden', 900, 400, null);
		const row = box('auto', 900, 40, wrap);
		expect(scrollParent(asEl(row))).toBe(page);
	});
});

describe('touchDrag autoscroll', () => {
	const VIEW_H = 800;
	let frames: Map<number, FrameRequestCallback>;
	let nextFrame: number;
	let win: EventTarget;
	let tree: FakeBox & {
		scrollTop: number;
		getBoundingClientRect: () => DOMRect;
		scrollBy: (opts: ScrollToOptions) => void;
	};
	let node: EventTarget;

	function pointer(type: string, x: number, y: number) {
		return Object.assign(new Event(type), {
			pointerType: 'touch',
			isPrimary: true,
			pointerId: 7,
			clientX: x,
			clientY: y
		});
	}

	/** Run the frames queued so far (not the ones they queue). */
	function runFrames() {
		const queued = [...frames.values()];
		frames.clear();
		for (const cb of queued) cb(0);
	}

	beforeEach(() => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
		frames = new Map();
		nextFrame = 1;
		win = new EventTarget();
		Object.assign(win, { innerHeight: VIEW_H });
		vi.spyOn(win, 'addEventListener');
		vi.spyOn(win, 'removeEventListener');
		vi.stubGlobal('window', win);
		vi.stubGlobal('navigator', {});
		vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
			frames.set(nextFrame, cb);
			return nextFrame++;
		});
		vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
		vi.stubGlobal('getComputedStyle', (el: FakeBox) => ({ overflowY: el.overflowY }));
		vi.stubGlobal('document', {
			scrollingElement: null,
			body: { appendChild: () => {}, classList: { add: () => {}, remove: () => {} } }
		});

		// The sidebar tree: 600px tall on screen from y=100, 2000px of rows.
		tree = Object.assign(box('auto', 2000, 600, null), {
			scrollTop: 500,
			getBoundingClientRect: () => ({ top: 100, bottom: 700, left: 0, right: 300 }) as DOMRect,
			scrollBy(opts: ScrollToOptions) {
				const max = tree.scrollHeight - tree.clientHeight;
				tree.scrollTop = Math.min(max, Math.max(0, tree.scrollTop + (opts.top ?? 0)));
			}
		});
		node = Object.assign(new EventTarget(), {
			parentElement: tree,
			getBoundingClientRect: () => ({ width: 280 }) as DOMRect,
			cloneNode: () => ({ style: {}, remove: () => {} })
		});
	});
	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	function start(x: number, y: number) {
		const calls = {
			onStart: vi.fn(),
			onMove: vi.fn(),
			onDrop: vi.fn(),
			onCancel: vi.fn()
		};
		const action = touchDrag(node as unknown as HTMLElement, calls);
		node.dispatchEvent(pointer('pointerdown', x, y));
		vi.advanceTimersByTime(300);
		expect(calls.onStart).toHaveBeenCalledOnce();
		return { calls, action };
	}

	it('scrolls the tree while the finger holds near an edge, re-hit-testing each step', () => {
		const { calls } = start(150, 400);
		win.dispatchEvent(pointer('pointermove', 150, 110));
		expect(frames.size).toBe(1);
		calls.onMove.mockClear();

		runFrames();
		runFrames();
		expect(tree.scrollTop).toBeLessThan(500);
		expect(calls.onMove).toHaveBeenCalledTimes(2);
		expect(calls.onMove).toHaveBeenLastCalledWith(150, 110);

		// Further moves inside the zone do not start a second loop.
		win.dispatchEvent(pointer('pointermove', 150, 108));
		expect(frames.size).toBe(1);

		// Back in the middle: the loop ends on its next frame.
		win.dispatchEvent(pointer('pointermove', 150, 400));
		const at = tree.scrollTop;
		runFrames();
		expect(tree.scrollTop).toBe(at);
		expect(frames.size).toBe(0);
	});

	it('stops at the end of the content and ignores a finger off to the side', () => {
		const { calls } = start(150, 400);
		tree.scrollTop = 1400; // already at the bottom
		win.dispatchEvent(pointer('pointermove', 150, 695));
		calls.onMove.mockClear();
		runFrames();
		expect(calls.onMove).not.toHaveBeenCalled();
		expect(frames.size).toBe(0);

		tree.scrollTop = 500;
		win.dispatchEvent(pointer('pointermove', 600, 695)); // over the editor
		expect(frames.size).toBe(0);
	});

	it('holds window listeners only for the gesture', () => {
		const { calls } = start(150, 400);
		const added = vi.mocked(win.addEventListener).mock.calls.map((c) => c[0]);
		expect(added).toEqual(['pointermove', 'pointerup', 'pointercancel']);

		win.dispatchEvent(pointer('pointermove', 150, 690));
		win.dispatchEvent(pointer('pointerup', 150, 690));
		expect(calls.onDrop).toHaveBeenCalledOnce();
		expect(frames.size).toBe(0);
		const removed = vi.mocked(win.removeEventListener).mock.calls.map((c) => c[0]);
		expect(removed).toEqual(['pointermove', 'pointerup', 'pointercancel']);

		// Detached: later pointer traffic reaches nothing.
		calls.onMove.mockClear();
		win.dispatchEvent(pointer('pointermove', 150, 300));
		expect(calls.onMove).not.toHaveBeenCalled();
	});

	it('detaches everything when the row unmounts mid-drag', () => {
		const { calls, action } = start(150, 400);
		win.dispatchEvent(pointer('pointermove', 150, 110));
		action.destroy();
		expect(calls.onCancel).toHaveBeenCalledOnce();
		expect(frames.size).toBe(0);
		calls.onMove.mockClear();
		win.dispatchEvent(pointer('pointermove', 150, 300));
		expect(calls.onMove).not.toHaveBeenCalled();
	});

	it('a finger that moves before the press matures stays a scroll', () => {
		const calls = { onStart: vi.fn(), onMove: vi.fn(), onDrop: vi.fn(), onCancel: vi.fn() };
		touchDrag(node as unknown as HTMLElement, calls);
		node.dispatchEvent(pointer('pointerdown', 150, 400));
		win.dispatchEvent(pointer('pointermove', 150, 380));
		vi.advanceTimersByTime(300);
		expect(calls.onStart).not.toHaveBeenCalled();
		expect(vi.mocked(win.removeEventListener)).toHaveBeenCalledWith('pointermove', expect.any(Function));
	});
});
