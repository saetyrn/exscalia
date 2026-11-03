// @vitest-environment jsdom
import type { OnNavigate } from '@sveltejs/kit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	isSuperseded,
	requestSlide,
	SLIDE_ATTR,
	slideIn,
	slideNavigation,
	sliding,
	takeSlide,
	type Direction
} from './transition';

const ORIGIN = 'http://app.test';
const MENU = '/recipes';
const A = '/recipes/recipes/a.md';
const B = '/recipes/recipes/b.md';
const FRAME = 16;

function target(href: string) {
	return { url: new URL(href, ORIGIN), params: {}, route: { id: null }, scroll: null };
}

function nav(type: 'goto' | 'popstate', href: string): Pick<OnNavigate, 'type' | 'to'> {
	return { type, to: target(href) } as unknown as Pick<OnNavigate, 'type' | 'to'>;
}

describe('slide requests', () => {
	it('hands the direction to the navigation it names, once', () => {
		requestSlide('forward', A, 1000);
		expect(takeSlide(nav('goto', A), 1200)).toBe('forward');
		expect(takeSlide(nav('goto', A), 1300)).toBeNull();
	});

	it('gives a history request to the next step through history only', () => {
		requestSlide('back', null, 1000);
		expect(takeSlide(nav('popstate', MENU), 1100)).toBe('back');
		requestSlide('back', null, 1000);
		expect(takeSlide(nav('goto', MENU), 1100)).toBeNull();
	});

	it('does not slide another navigation, and is spent by it', () => {
		requestSlide('forward', A, 1000);
		expect(takeSlide(nav('popstate', MENU), 1100)).toBeNull();
		expect(takeSlide(nav('goto', A), 1100)).toBeNull();
		requestSlide('forward', A, 1000);
		expect(takeSlide(nav('goto', B), 1100)).toBeNull();
	});

	it('compares the href as the router resolves it', () => {
		requestSlide('forward', '/recipes/recipes/Lemon%20Tart.md', 1000);
		expect(takeSlide(nav('goto', '/recipes/recipes/Lemon Tart.md'), 1100)).toBe('forward');
		requestSlide('forward', '/recipes?q=a', 1000);
		expect(takeSlide(nav('goto', '/recipes?q=b'), 1100)).toBeNull();
	});

	it('drops a request no navigation picked up in time', () => {
		requestSlide('back', null, 1000);
		expect(takeSlide(nav('popstate', MENU), 2500)).toBeNull();
	});
});

/** document.startViewTransition as Chromium runs it: the old state is captured
 * at the next frame, then the update runs, and the animation plays once it
 * resolves. skipTransition(), or a newer transition, skips it: the update
 * still runs, `ready` rejects, `finished` waits for the update. */
class FakeTransition {
	static active: FakeTransition | null = null;
	static started: FakeTransition[] = [];
	/** The browser never gets round to the capture. */
	static stall = false;

	phase: 'capture' | 'update' | 'animating' | 'done' = 'capture';
	skipped = false;
	ready: Promise<void>;
	finished: Promise<void>;
	private resolveReady!: () => void;
	private rejectReady!: (e: Error) => void;
	private resolveFinished!: () => void;
	private updated: Promise<void> | null = null;

	constructor(private update: () => Promise<void>) {
		FakeTransition.active?.skipTransition();
		FakeTransition.active = this;
		FakeTransition.started.push(this);
		this.ready = new Promise((resolve, reject) => {
			this.resolveReady = resolve;
			this.rejectReady = reject;
		});
		this.ready.catch(() => undefined);
		this.finished = new Promise((resolve) => (this.resolveFinished = resolve));
		if (!FakeTransition.stall) setTimeout(() => this.capture(), FRAME);
	}

	private runUpdate(): Promise<void> {
		this.updated ??= Promise.resolve()
			.then(() => this.update())
			.then(
				() => undefined,
				() => undefined
			);
		return this.updated;
	}

	private capture() {
		if (this.phase !== 'capture') return;
		this.phase = 'update';
		void this.runUpdate().then(() => {
			if (this.skipped) return;
			this.phase = 'animating';
			this.resolveReady();
			setTimeout(() => this.finish(), 520);
		});
	}

	private finish() {
		if (this.phase === 'done') return;
		this.phase = 'done';
		if (FakeTransition.active === this) FakeTransition.active = null;
		void this.runUpdate().then(() => this.resolveFinished());
	}

	skipTransition() {
		if (this.phase === 'done') return;
		this.skipped = true;
		this.rejectReady(new Error('AbortError'));
		this.finish();
	}
}

/** SvelteKit's client router cut down to the steps that matter here (navigate()
 * in @sveltejs/kit/src/runtime/client/client.js): a new navigation takes the
 * token; the route loads, and its props are worked out against the page that
 * is current then, carrying data only where it differs; a goto pushes its
 * history entry; every onNavigate callback is awaited (a rejection fails the
 * navigation there); the navigation becomes current and renders without
 * checking the token; two ticks later a superseded one is aborted. */
class Router {
	url: string;
	screen: string;
	/** The data the root hands the page, which a render without data keeps. */
	data: string;
	renders: { to: string; sliding: boolean }[] = [];
	private current: string;
	private token = {};

	constructor(start: string) {
		this.url = this.screen = this.current = start;
		this.data = dataOf(start);
	}

	/** The page on screen has its own data. */
	get consistent() {
		return this.data === dataOf(this.screen);
	}

	goto(to: string, load = 0) {
		return this.navigate('goto', to, load);
	}

	/** Back or Forward in the browser: the URL moves first, then the router runs. */
	traverse(to: string, delta: number, load = 0) {
		this.url = to;
		return this.navigate('popstate', to, load, delta);
	}

	private async navigate(type: 'goto' | 'popstate', to: string, load: number, delta?: number) {
		const token = (this.token = {});
		let fulfil!: () => void;
		let reject!: (e: Error) => void;
		const complete = new Promise<void>((resolve, fail) => {
			fulfil = resolve;
			reject = fail;
		});
		complete.catch(() => undefined);
		const navigation = {
			type,
			from: target(this.url),
			to: target(to),
			delta,
			willUnload: false,
			complete
		} as unknown as OnNavigate;
		if (load) await new Promise((resolve) => setTimeout(resolve, load));
		else await Promise.resolve();
		if (token !== this.token) return reject(new Error('navigation aborted'));
		const data = to === this.current ? null : dataOf(to);
		if (type === 'goto') this.url = to;
		await Promise.all([slideNavigation(navigation)]);
		this.current = to;
		this.screen = to;
		if (data) this.data = data;
		this.renders.push({ to, sliding: sliding() });
		await Promise.resolve();
		await Promise.resolve();
		if (token !== this.token) return reject(new Error('navigation aborted'));
		fulfil();
	}
}

const dataOf = (href: string) => `data of ${href}`;

/** What nav.ts does: ask, navigate, and settle quietly when superseded. */
function slideGoto(router: Router, dir: Direction, href: string) {
	requestSlide(dir, href);
	return router.goto(href).catch((error: unknown) => {
		if (!isSuperseded(error)) throw error;
	});
}
function slideBack(router: Router, href: string) {
	requestSlide('back', null);
	return router.traverse(href, -1);
}

/** Whether a promise has settled, read after the microtasks have run. */
function watch(promise: Promise<unknown>) {
	const state = { settled: false };
	promise.then(
		() => (state.settled = true),
		() => (state.settled = true)
	);
	return state;
}

const tick = (ms = 0) => vi.advanceTimersByTimeAsync(ms);
const rendered = (router: Router) => router.renders.map((r) => r.to);

let reduce = false;
type LooseDoc = { startViewTransition?: (update: () => Promise<void>) => FakeTransition };

describe('slides and overlapping navigations', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		reduce = false;
		FakeTransition.active = null;
		FakeTransition.started = [];
		FakeTransition.stall = false;
		(document as unknown as LooseDoc).startViewTransition = (update) => new FakeTransition(update);
		window.matchMedia = ((query: string) => ({
			matches: reduce && query.includes('reduce'),
			media: query
		})) as unknown as typeof window.matchMedia;
	});

	afterEach(async () => {
		// Let every slide run out, so none carries into the next test.
		await vi.runAllTimersAsync();
		vi.useRealTimers();
		expect(sliding()).toBe(false);
	});

	it('slides forward: holds the page for the capture, then lands it', async () => {
		const router = new Router(MENU);
		void slideGoto(router, 'forward', A);
		await tick();
		expect(router.url).toBe(A);
		expect(router.screen).toBe(MENU);
		expect(document.documentElement.getAttribute(SLIDE_ATTR)).toBe('forward');
		await tick(FRAME);
		expect(router.screen).toBe(A);
		expect(FakeTransition.started[0].phase).toBe('animating');
		await tick(1200);
		expect(sliding()).toBe(false);
		expect(rendered(router)).toEqual([A]);
		expect(router.consistent).toBe(true);
	});

	// The race the menu's author found: Back while the old pane is still being
	// captured rendered the menu, then the held forward rendered the recipe
	// over it.
	it.each([0, 5, FRAME - 1, FRAME, FRAME + 4, 100, 300, 700])(
		'Back %i ms into a forward slide ends on the page the URL names',
		async (ms) => {
			const router = new Router(MENU);
			void slideGoto(router, 'forward', A);
			await tick(ms);
			void router.traverse(MENU, -1);
			await vi.runAllTimersAsync();
			expect(router.url).toBe(MENU);
			expect(router.screen).toBe(MENU);
			expect(rendered(router).at(-1)).toBe(MENU);
			expect(router.consistent).toBe(true);
		}
	);

	it('Back mid-capture: the held forward never renders, and its goto settles', async () => {
		const router = new Router(MENU);
		const forward = watch(slideGoto(router, 'forward', A));
		await tick();
		void router.traverse(MENU, -1);
		await tick();
		expect(rendered(router)).toEqual([MENU]);
		// The browser's Back does not slide, so the menu fades in as usual.
		expect(router.renders[0].sliding).toBe(false);
		expect(FakeTransition.started).toHaveLength(1);
		expect(FakeTransition.started[0].skipped).toBe(true);
		expect(forward.settled).toBe(true);
		await vi.runAllTimersAsync();
		// The skipped transition's update still runs; it must not let the held
		// navigation go after all.
		expect(rendered(router)).toEqual([MENU]);
	});

	it('Back during the forward animation skips the rest of it', async () => {
		const router = new Router(MENU);
		void slideGoto(router, 'forward', A);
		await tick(FRAME + 100);
		void router.traverse(MENU, -1);
		await tick();
		expect(FakeTransition.started[0].skipped).toBe(true);
		expect(router.screen).toBe(MENU);
		expect(router.consistent).toBe(true);
		expect(sliding()).toBe(false);
	});

	// Letting the held back slide render first left the recipe page holding
	// the menu's data: the Forward's props were worked out against the recipe.
	it.each([0, 5, FRAME - 1, FRAME + 100])(
		'Forward in the browser %i ms into a back slide ends on its page, with its data',
		async (ms) => {
			const router = new Router(A);
			const back = watch(slideBack(router, MENU));
			await tick(ms);
			void router.traverse(A, 1);
			await vi.runAllTimersAsync();
			expect(router.url).toBe(A);
			expect(router.screen).toBe(A);
			expect(router.consistent).toBe(true);
			// Held when superseded: a step through history is never let go.
			expect(back.settled).toBe(ms >= FRAME);
		}
	);

	it('a forward slide during a back slide takes over and slides itself', async () => {
		const router = new Router(A);
		void slideBack(router, MENU);
		await tick(5);
		void slideGoto(router, 'forward', B);
		await tick();
		expect(rendered(router)).toEqual([]);
		expect(document.documentElement.getAttribute(SLIDE_ATTR)).toBe('forward');
		await tick(FRAME);
		expect(rendered(router)).toEqual([B]);
		expect(FakeTransition.started.map((t) => t.skipped)).toEqual([true, false]);
		await vi.runAllTimersAsync();
		expect(router.screen).toBe(B);
		expect(router.consistent).toBe(true);
	});

	it('a second forward during a forward slides the second; the first never renders', async () => {
		const router = new Router(MENU);
		const first = watch(slideGoto(router, 'forward', A));
		await tick(5);
		void slideGoto(router, 'forward', B);
		await tick();
		expect(first.settled).toBe(true);
		expect(FakeTransition.started).toHaveLength(2);
		expect(FakeTransition.started[0].skipped).toBe(true);
		// The first transition is over, and its end leaves the second's slide alone.
		await tick(FRAME);
		expect(rendered(router)).toEqual([B]);
		expect(document.documentElement.getAttribute(SLIDE_ATTR)).toBe('forward');
		expect(FakeTransition.started[1].phase).toBe('animating');
		await vi.runAllTimersAsync();
		expect(router.url).toBe(B);
		expect(router.screen).toBe(B);
		expect(router.consistent).toBe(true);
	});

	it('a second forward during the first one’s animation keeps its own slide to the end', async () => {
		const router = new Router(MENU);
		void slideGoto(router, 'forward', A);
		await tick(FRAME + 200);
		void slideGoto(router, 'forward', B);
		await tick(FRAME);
		expect(FakeTransition.started[0].skipped).toBe(true);
		await tick(400);
		expect(document.documentElement.getAttribute(SLIDE_ATTR)).toBe('forward');
		await tick(200);
		expect(sliding()).toBe(false);
		expect(rendered(router)).toEqual([A, B]);
		expect(router.consistent).toBe(true);
	});

	it.each([5, FRAME + 100])(
		'leaving the section %i ms into a slide shows the new section, unslid',
		async (ms) => {
			const router = new Router(MENU);
			void slideGoto(router, 'forward', A);
			await tick(ms);
			void router.goto('/tasks');
			await vi.runAllTimersAsync();
			expect(router.screen).toBe('/tasks');
			expect(router.renders.at(-1)).toEqual({ to: '/tasks', sliding: false });
			expect(router.consistent).toBe(true);
		}
	);

	it('a slower navigation that lands after the slide ended still renders last', async () => {
		const router = new Router(MENU);
		void slideGoto(router, 'forward', A);
		await tick(5);
		void router.traverse(MENU, -1, 2000);
		await vi.runAllTimersAsync();
		expect(rendered(router)).toEqual([A, MENU]);
		expect(router.screen).toBe(MENU);
		expect(router.consistent).toBe(true);
	});

	// A live event refreshes the cache, and the open page redraws in place: no
	// navigation, so the slide carries on and the next one is unaffected.
	it('a cache refresh mid-slide neither ends nor restarts it', async () => {
		const router = new Router(MENU);
		void slideGoto(router, 'forward', A);
		await tick(FRAME + 100);
		router.screen = A; // redrawn with the fresh copy
		await tick(200);
		expect(document.documentElement.getAttribute(SLIDE_ATTR)).toBe('forward');
		expect(FakeTransition.started[0].skipped).toBe(false);
		await vi.runAllTimersAsync();
		void slideBack(router, MENU);
		await tick(FRAME);
		expect(FakeTransition.started).toHaveLength(2);
		expect(router.screen).toBe(MENU);
	});

	it('lets the router go if the browser never runs the update', async () => {
		FakeTransition.stall = true;
		const router = new Router(MENU);
		void slideGoto(router, 'forward', A);
		await tick(999);
		expect(router.screen).toBe(MENU);
		await tick(1);
		expect(router.screen).toBe(A);
		FakeTransition.started[0].skipTransition();
	});

	it('does not slide a navigation nobody asked to slide', async () => {
		const router = new Router(MENU);
		void router.goto(A);
		await tick();
		expect(router.screen).toBe(A);
		expect(FakeTransition.started).toHaveLength(0);
		expect(sliding()).toBe(false);
	});

	it('gives reduced motion no slide', async () => {
		reduce = true;
		const router = new Router(MENU);
		void slideGoto(router, 'forward', A);
		await tick();
		expect(router.screen).toBe(A);
		expect(FakeTransition.started).toHaveLength(0);
		expect(sliding()).toBe(false);
	});

	describe('without View Transitions', () => {
		beforeEach(() => {
			delete (document as unknown as LooseDoc).startViewTransition;
		});

		function pane() {
			const node = document.createElement('div');
			const animate = vi.fn();
			node.animate = animate as unknown as typeof node.animate;
			return { node, animate };
		}

		it('renders at once and slides the new pane in on its own', async () => {
			const router = new Router(MENU);
			void slideGoto(router, 'forward', A);
			await tick();
			expect(router.screen).toBe(A);
			expect(sliding()).toBe(true);
			const { node, animate } = pane();
			slideIn(node);
			expect(animate).toHaveBeenCalledOnce();
			expect(animate.mock.calls[0][0][0]).toEqual({ transform: 'translateX(100%)' });
			await tick(520);
			expect(sliding()).toBe(false);
		});

		it('Back straight after a forward slide ends on the menu, which does not slide', async () => {
			const router = new Router(MENU);
			void slideGoto(router, 'forward', A);
			void router.traverse(MENU, -1);
			await tick();
			expect(router.screen).toBe(MENU);
			expect(router.consistent).toBe(true);
			expect(sliding()).toBe(false);
			const { node, animate } = pane();
			slideIn(node);
			expect(animate).not.toHaveBeenCalled();
		});

		it('an earlier slide’s end leaves a later one alone', async () => {
			const router = new Router(MENU);
			void slideGoto(router, 'forward', A);
			await tick(300);
			slideIn(pane().node);
			void slideGoto(router, 'forward', B);
			await tick();
			const { node, animate } = pane();
			slideIn(node);
			expect(animate).toHaveBeenCalledOnce();
			// The first slide's timer runs out while the second is still going.
			await tick(300);
			expect(sliding()).toBe(true);
			await tick(220);
			expect(sliding()).toBe(false);
			expect(router.screen).toBe(B);
		});

		// Found in review: a navigation that failed mounted no pane, and the
		// next pane to mount, asked or not, slid in.
		it('a navigation that fails leaves no slide for the next pane', async () => {
			requestSlide('forward', A);
			const complete = Promise.reject(new Error('navigation aborted'));
			complete.catch(() => undefined);
			slideNavigation({ type: 'goto', to: target(A), complete } as unknown as OnNavigate);
			expect(sliding()).toBe(true);
			await tick();
			expect(sliding()).toBe(false);
			const { node, animate } = pane();
			slideIn(node);
			expect(animate).not.toHaveBeenCalled();
		});
	});
});
