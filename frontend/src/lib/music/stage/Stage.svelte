<!--
	The 3D stage (lib/music/stage/scene.ts). three.js is imported here on
	first mount only, so it loads with the Music page and nowhere else. The
	render loop stops while the tab is hidden; reduced motion holds the object
	still. Covers come through apiFetch, which signs the desktop app's
	requests; a page-relative URL would point at the app, not the server.
-->
<script lang="ts">
	import { onMount } from 'svelte';
	import { apiFetch } from '$lib/api';
	import type { StageObject, StageScene } from './scene';

	let { object, playing = false }: { object: StageObject; playing?: boolean } = $props();

	let canvas: HTMLCanvasElement | undefined = $state();
	let box: HTMLDivElement | undefined = $state();
	let stage = $state<StageScene | null>(null);
	let failed = $state(false);

	onMount(() => {
		let alive = true;
		let frame = 0;
		let observer: ResizeObserver | null = null;
		const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
		const onMotion = () => stage?.setReducedMotion(motion.matches);

		void (async () => {
			try {
				const [{ StageScene }, THREE] = await Promise.all([import('./scene'), import('three')]);
				if (!alive || !canvas || !box) return;
				const textureLoader = new THREE.TextureLoader();
				const load = async (url: string) => {
					try {
						let src = url;
						if (url.startsWith('/api/')) {
							const res = await apiFetch(url);
							if (!res.ok) return null;
							src = URL.createObjectURL(await res.blob());
						}
						const texture = await textureLoader.loadAsync(src);
						if (src !== url) URL.revokeObjectURL(src);
						texture.colorSpace = THREE.SRGBColorSpace;
						texture.anisotropy = 8;
						return texture;
					} catch {
						return null;
					}
				};
				// Glass refracts the page behind it: the stage's own backdrop
				// colour when the page sets one, else the app's.
				const css = (el: Element, name: string) => getComputedStyle(el).getPropertyValue(name).trim();
				const backdrop = css(box, '--stage-backdrop') || css(document.documentElement, '--bg') || '#e7e9f0';
				const scene = new StageScene(canvas, load, { reducedMotion: motion.matches, backdrop });
				stage = scene;
				const size = () => scene.resize(box!.clientWidth, box!.clientHeight);
				size();
				observer = new ResizeObserver(size);
				observer.observe(box);
				motion.addEventListener('change', onMotion);
				const loop = () => {
					frame = requestAnimationFrame(loop);
					if (document.visibilityState === 'visible') scene.frame();
				};
				loop();
			} catch {
				failed = true;
			}
		})();

		return () => {
			alive = false;
			cancelAnimationFrame(frame);
			observer?.disconnect();
			motion.removeEventListener('change', onMotion);
			stage?.dispose();
			stage = null;
		};
	});

	$effect(() => {
		void stage?.show(object);
	});

	$effect(() => {
		stage?.setPlaying(playing);
	});
</script>

<div class="stage" bind:this={box} aria-hidden="true">
	{#if !failed}<canvas bind:this={canvas}></canvas>{/if}
</div>

<style>
	.stage {
		position: relative;
		width: 100%;
		height: 100%;
	}
	canvas {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: block;
	}
</style>
