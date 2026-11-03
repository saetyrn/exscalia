// Page slides between recipe screens, after the recipe app's transition.ts.
// Forward slides the new page in from the right while the old one drifts off
// left; back reverses it. Only the recipes pane moves: the sidebar, tab strip
// and bars stay put.
//
// How it meets SvelteKit: nav.ts calls requestSlide() just before its goto or
// history.back(), and the recipes layout registers slideNavigation() with
// onNavigate. That runs before the router swaps the page, so a View Transition
// can capture the old pane, let the navigation finish (the root layout's
// {#key} remounts the whole recipes layout and page, which is fine here: the
// old pane is captured as a picture first), and capture the new one. The pane
// carries `view-transition-name: recipe-page` only while
// html[data-recipe-slide] is set, and recipes.css holds the keyframes. The
// root layout skips its own route fade while that attribute is up, or the new
// page would fade in inside the slide.
//
// Navigations overlap: the browser's Back while the old pane is still being
// captured, a second slide before the first lands, a sidebar link mid-slide.
// SvelteKit renders a navigation as soon as its onNavigate callbacks resolve
// and does not check first whether a newer one has started, so a slide that
// held its navigation back would render it over the newer page (the URL at
// /recipes, the recipe on screen). Letting the held one render first is no
// better: the newer navigation's props were worked out against the page
// before it, so it would then render with the wrong data. So a slide request
// names the navigation it is for, each slide belongs to that navigation, and
// whichever navigation comes next supersedes it: its transition is skipped
// and, if it is still holding its navigation, that navigation never renders.
// A held goto fails with a superseded error, which nav.ts swallows the way
// SvelteKit settles any navigation a newer one aborted; a held step through
// history, which only the router itself awaits, is simply never let go.
//
// Browsers without the API get the simpler slide-in the original used: the new
// pane animates in on its own (slideIn below). Reduced motion gets no slide.
// A navigation nobody asked to slide (a sidebar link, the browser's Back)
// does not slide.

import type { OnNavigate } from '@sveltejs/kit';

export type Direction = 'forward' | 'back';

export const SLIDE_ATTR = 'data-recipe-slide';
const DURATION = 520;
const EASE = 'cubic-bezier(0.22, 1, 0.36, 1)';
/** The longest the router waits on a slide's capture. */
const HOLD_MS = 1000;
/** How long a slide request waits for its navigation. */
const REQUEST_MS = 1000;
const SUPERSEDED = 'recipe slide superseded';

interface ViewTransitionLike {
	finished: Promise<void>;
	ready: Promise<void>;
	skipTransition?: () => void;
}

type DocWithVT = Document & {
	startViewTransition?: (update: () => Promise<void>) => ViewTransitionLike;
};

/** A slide on its way: from its navigation reaching onNavigate until its
 * animation ends. */
interface Slide {
	dir: Direction;
	navigation: OnNavigate;
	/** Lets the navigation render, unless the slide was superseded first. */
	release: () => void;
	/** Keeps a navigation not yet let go from ever rendering. */
	abort: () => void;
	vt: ViewTransitionLike | null;
}

let pending: { dir: Direction; to: string | null; at: number } | null = null;
let current: Slide | null = null;
/** Set for the fallback path: the slide whose pane mounts next. */
let entering: Slide | null = null;

/** Ask for a navigation to slide: the goto to `to`, or with `to` null the
 * next step through history (history.back()). The next navigation takes the
 * request whether or not it is that one, and a request nothing takes within a
 * second is dropped, so a cancelled goto cannot make a later click slide. */
export function requestSlide(dir: Direction, to: string | null, now = Date.now()): void {
	pending = { dir, to, at: now };
}

/** The direction requested for this navigation, consumed; null when it is
 * not the navigation the request named. */
export function takeSlide(
	navigation: Pick<OnNavigate, 'type' | 'to'>,
	now = Date.now()
): Direction | null {
	const request = pending;
	pending = null;
	const target = navigation.to?.url;
	if (!request || !target || now - request.at >= REQUEST_MS) return null;
	if (request.to === null) return navigation.type === 'popstate' ? request.dir : null;
	if (navigation.type !== 'goto') return null;
	const asked = new URL(request.to, target);
	return asked.pathname === target.pathname && asked.search === target.search
		? request.dir
		: null;
}

function reducedMotion(): boolean {
	return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** True for the error a superseded slide's goto fails with. */
export function isSuperseded(error: unknown): boolean {
	return error instanceof Error && error.message === SUPERSEDED;
}

/** Resolves once `promise` settles, or after `ms` at the latest. */
function settledWithin(promise: Promise<unknown>, ms: number): Promise<void> {
	return new Promise((resolve) => {
		const timer = setTimeout(resolve, ms);
		const done = () => {
			clearTimeout(timer);
			resolve();
		};
		promise.then(done, done);
	});
}

/** The slide ran its course, or its navigation failed. */
function end(slide: Slide): void {
	if (current !== slide) return;
	current = null;
	if (entering === slide) entering = null;
	document.documentElement.removeAttribute(SLIDE_ATTR);
}

/** A newer navigation arrived: the old slide's picture goes, and a navigation
 * it still holds never renders. */
function supersede(slide: Slide): void {
	current = null;
	if (entering === slide) entering = null;
	document.documentElement.removeAttribute(SLIDE_ATTR);
	slide.abort();
	slide.vt?.skipTransition?.();
}

/** True while a recipe slide is running. */
export function sliding(): boolean {
	return typeof document !== 'undefined' && document.documentElement.hasAttribute(SLIDE_ATTR);
}

/** True from a slide's request until its animation ends: the page on screen
 * is on its way out (the router is still loading the next one, or the old
 * pane is held for the capture), and a key would act on it. */
export function leaving(now = Date.now()): boolean {
	return sliding() || (pending !== null && now - pending.at < REQUEST_MS);
}

/** For onNavigate in the recipes layout. */
export function slideNavigation(navigation: OnNavigate): Promise<void> | undefined {
	const dir = takeSlide(navigation);
	if (current) supersede(current);
	if (!dir || reducedMotion()) return;
	const doc = document as DocWithVT;

	if (!doc.startViewTransition) {
		const slide: Slide = { dir, navigation, release: () => {}, abort: () => {}, vt: null };
		current = entering = slide;
		document.documentElement.setAttribute(SLIDE_ATTR, dir);
		// A navigation that fails mounts no pane, and must not leave its slide
		// for the next pane that does.
		navigation.complete.then(
			() => setTimeout(() => end(slide), DURATION),
			() => end(slide)
		);
		return;
	}

	let aborted = false;
	let resolve!: () => void;
	let reject!: (error: Error) => void;
	const held = new Promise<void>((done, fail) => {
		resolve = done;
		reject = fail;
	});
	const slide: Slide = {
		dir,
		navigation,
		release: () => {
			if (!aborted) resolve();
		},
		abort: () => {
			aborted = true;
			if (navigation.type !== 'popstate') reject(new Error(SUPERSEDED));
		},
		vt: null
	};
	current = slide;
	document.documentElement.setAttribute(SLIDE_ATTR, dir);

	// Never leave the router waiting if the browser drops the transition
	// before it runs the update.
	const guard = setTimeout(slide.release, HOLD_MS);
	try {
		slide.vt = doc.startViewTransition(async () => {
			clearTimeout(guard);
			slide.release();
			// A slow load must not freeze the page under the old picture.
			await settledWithin(navigation.complete, 600);
		});
	} catch {
		clearTimeout(guard);
		slide.release();
		end(slide);
		return held;
	}
	// A transition the browser skips (the tab hidden mid-capture) still runs
	// the update, so its navigation renders, just without the slide.
	slide.vt.ready.catch(() => undefined);
	slide.vt.finished.catch(() => undefined).finally(() => end(slide));
	return held;
}

/** Action on the recipes pane: plays the fallback slide-in when the browser
 * has no View Transitions. A no-op otherwise, and for a superseded slide. */
export function slideIn(node: HTMLElement): void {
	const slide = entering;
	entering = null;
	if (!slide || slide !== current || typeof node.animate !== 'function') return;
	node.animate(
		slide.dir === 'forward'
			? [{ transform: 'translateX(100%)' }, { transform: 'none' }]
			: [
					{ transform: 'translateX(-28%)', opacity: 0 },
					{ transform: 'none', opacity: 1 }
				],
		{ duration: 450, easing: EASE }
	);
}
