<script module lang="ts">
	// The root layout mounts a page twice as it arrives (in the old {#key}
	// block, then in its own), so the arrival checks below run twice back to
	// back. The two share one request.
	const arriving = new Map<string, Promise<unknown>>();
	function shared<T>(key: string, call: () => Promise<T>): Promise<T> {
		const running = arriving.get(key);
		if (running) return running as Promise<T>;
		const request = call().finally(() => arriving.delete(key));
		arriving.set(key, request);
		return request;
	}
</script>

<script lang="ts">
	// The dish in coloured ASCII art above the title, after the recipe app's
	// AsciiArt. Drawings run on the server in the background and can take
	// minutes, so on arrival this asks whether one is in progress, shows a
	// blinking "drawing_" while it is, and checks in until it ends. The new art
	// arrives through the recipe cache: the live listener refreshes it, or the
	// last check here loads the recipe. Redraw (and Draw, for a recipe with
	// none) shows only when the AI step can run.
	//
	// The art and status routes may be missing (an older server) or failing;
	// any error from them reads as "no AI, nothing in progress", quietly.
	import { api } from '$lib/api';
	import ArtPre from './ArtPre.svelte';
	import { loadRecipe } from './cache.svelte';

	const POLL_MS = 3000;
	const GIVE_UP_MS = 20 * 60 * 1000; // max thinking can take many minutes

	let {
		path,
		title,
		art,
		colors = '',
		palette = {}
	}: {
		path: string;
		title: string;
		art: string;
		colors?: string;
		palette?: Record<string, string>;
	} = $props();

	let ai = $state(false);
	let drawing = $state(false);
	let starting = $state(false);
	let failed = $state('');
	let since = 0;
	// The page hands a fresh recipe object on every refresh; the checks below
	// follow the path itself, so only a different recipe starts them again.
	let subject = $derived(path);

	// On arrival: can the AI draw, and is a drawing already running (this
	// recipe was just made, or a redraw was started elsewhere) or did the
	// last one fail (a rejected key fails before a new recipe's page opens)?
	$effect(() => {
		const p = subject;
		let live = true;
		shared('status', () => api.recipesStatus()).then(
			(s) => {
				if (live) ai = s?.ai === true;
			},
			() => {}
		);
		shared(`art:${p}`, () => api.recipesArtStatus(p)).then(
			(s) => {
				if (!live) return;
				if (!s?.drawing) {
					if (s?.error) failed = s.error;
					return;
				}
				drawing = true;
				since = Date.now();
			},
			() => {}
		);
		return () => {
			live = false;
		};
	});

	// While drawing: check in until it ends, then bring the new art in (or
	// say why there is none).
	$effect(() => {
		if (!drawing) return;
		const p = subject;
		let live = true;
		let timer: ReturnType<typeof setTimeout>;
		const check = async () => {
			try {
				const s = await api.recipesArtStatus(p);
				if (!live) return;
				if (s?.drawing && Date.now() - since < GIVE_UP_MS) {
					timer = setTimeout(check, POLL_MS);
					return;
				}
				drawing = false;
				if (s?.error) failed = s.error;
				else await loadRecipe(p);
			} catch {
				if (live) drawing = false;
			}
		};
		timer = setTimeout(check, POLL_MS);
		return () => {
			live = false;
			clearTimeout(timer);
		};
	});

	async function draw() {
		if (starting || drawing) return;
		failed = '';
		starting = true;
		try {
			await api.recipesStartArt(path);
			since = Date.now();
			drawing = true;
		} catch (e) {
			failed = (e as Error).message || 'Could not start a drawing';
		} finally {
			starting = false;
		}
	}
</script>

<!-- What a screen reader hears. A live region speaks when its text changes, not
     when it arrives holding it, so this one is always here and the lines below,
     which come and go with their text, are for the eye. -->
<p class="said" role="status">{drawing ? 'drawing' : failed}</p>

{#if art || drawing || ai || failed}
	<div class="wrap" class:empty={!art}>
		{#if art}
			<!-- Keyed, so a new drawing fades in as the first one did. -->
			{#key art}
				<ArtPre class="art{drawing ? ' busy' : ''}" {art} {colors} {palette} label={title} />
			{/key}
		{/if}
		{#if drawing}
			<p class="status rc-caption" aria-hidden="true">
				drawing<span class="caret">_</span>
			</p>
		{:else if ai}
			<button
				type="button"
				class="rc-link action"
				class:redraw={!!art}
				disabled={starting}
				onclick={() => void draw()}
			>
				{art ? 'Redraw' : 'Draw ASCII art'}
			</button>
		{/if}
		{#if failed}
			<p class="rc-error rc-small" aria-hidden="true">{failed}</p>
		{/if}
	</div>
{/if}

<style>
	.said {
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip-path: inset(50%);
		white-space: nowrap;
	}
	.wrap {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: center;
		margin-bottom: 18px;
	}
	.wrap.empty {
		min-height: 1.6em;
	}
	.wrap :global(pre.art) {
		max-width: 100%;
		overflow: hidden;
		font-size: clamp(7.5px, 2.2vw, 10px);
		animation: rc-art-in 0.6s cubic-bezier(0.2, 0.7, 0.2, 1) both;
	}
	.wrap :global(pre.art.busy) {
		opacity: 0.4;
	}
	@keyframes rc-art-in {
		from {
			opacity: 0;
			filter: blur(3px);
		}
	}
	.caret {
		animation: rc-caret 1s steps(1) infinite;
	}
	@keyframes rc-caret {
		50% {
			opacity: 0;
		}
	}
	.wrap .rc-error {
		margin: 6px 0 0;
		text-align: center;
	}

	/* Redraw shows when you point at the drawing or tab to it. */
	.redraw {
		margin-top: 6px;
		opacity: 0;
		transition:
			opacity 0.2s ease,
			color 0.15s ease;
	}
	.wrap:hover .redraw,
	.redraw:focus-visible {
		opacity: 1;
	}
	.action:hover:not(:disabled) {
		text-decoration: underline;
	}

	/* Touch has no hover: on phones and touch screens Redraw is always there,
	   at thumb size, as media's item page does with its hover-revealed Delete. */
	@media (max-width: 760px), (hover: none) {
		.redraw {
			margin-top: 0;
			opacity: 1;
		}
		.action {
			min-height: 44px;
			padding: 0 10px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.caret,
		.wrap :global(pre.art) {
			animation: none;
		}
		.redraw {
			transition: none;
		}
	}
</style>
