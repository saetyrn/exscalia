// Reduced-motion-aware stand-ins for svelte/transition and svelte/animate.
//
// Svelte 5 plays every transition and animate:flip through element.animate()
// (Web Animations), which the prefers-reduced-motion block in app.css cannot
// reach: animation-duration and transition-duration only govern CSS
// animations and transitions. So with Reduce Motion on, each of these returns
// a zero-length config and the element appears, leaves or moves in place;
// otherwise it is the stock function, untouched. Same names and signatures —
// import from '$lib/motion' instead of 'svelte/transition' / 'svelte/animate'
// and the directives stay exactly as they are.
import { flip as svelteFlip, type AnimationConfig, type FlipParams } from 'svelte/animate';
import {
	fade as svelteFade,
	fly as svelteFly,
	scale as svelteScale,
	slide as svelteSlide,
	type FadeParams,
	type FlyParams,
	type ScaleParams,
	type SlideParams,
	type TransitionConfig
} from 'svelte/transition';

export type {
	AnimationConfig,
	FadeParams,
	FlipParams,
	FlyParams,
	ScaleParams,
	SlideParams,
	TransitionConfig
};

// Read on every call, not cached: toggling the OS setting applies to the next
// transition without a reload.
const reduce = () =>
	typeof window !== 'undefined' &&
	!!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

export function fade(node: Element, params?: FadeParams): TransitionConfig {
	return reduce() ? { duration: 0 } : svelteFade(node, params);
}

export function fly(node: Element, params?: FlyParams): TransitionConfig {
	return reduce() ? { duration: 0 } : svelteFly(node, params);
}

export function slide(node: Element, params?: SlideParams): TransitionConfig {
	return reduce() ? { duration: 0 } : svelteSlide(node, params);
}

export function scale(node: Element, params?: ScaleParams): TransitionConfig {
	return reduce() ? { duration: 0 } : svelteScale(node, params);
}

export function flip(
	node: Element,
	fromTo: { from: DOMRect; to: DOMRect },
	params?: FlipParams
): AnimationConfig {
	return reduce() ? { duration: 0 } : svelteFlip(node, fromTo, params);
}
