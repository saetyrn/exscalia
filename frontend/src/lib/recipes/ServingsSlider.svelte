<script lang="ts">
	// Servings, after the recipe app's ServingsSlider: the label, the slider
	// and the number in one row, directly above the ingredients it scales. A
	// tick marks the servings the recipe was written for. The page holds the
	// value; every move reports the new one, and the factor is value / base.
	import { factorText, servingsMax } from './recipepage';

	let {
		base,
		value,
		onchange
	}: {
		/** The servings the recipe was written for. */
		base: number;
		value: number;
		onchange: (servings: number) => void;
	} = $props();

	const id = $props.id();
	const MIN = 1;
	let max = $derived(servingsMax(base));
	const pct = (n: number) => ((n - MIN) / (max - MIN)) * 100;
	let slider = $state<HTMLInputElement>();

	// Reset disappears once the value is the base; focus goes to the slider
	// rather than falling to the page.
	function reset() {
		onchange(base);
		slider?.focus();
	}
</script>

<section class="servings" aria-label="Servings">
	<div class="row">
		<label for={id} class="rc-caption">Servings</label>
		<div class="slider">
			<span class="base" style:left="{pct(base)}%" aria-hidden="true"></span>
			<input
				bind:this={slider}
				{id}
				type="range"
				min={MIN}
				{max}
				step="1"
				{value}
				style:--fill="{pct(value)}%"
				aria-valuetext="{value} servings"
				oninput={(e) => onchange(Number(e.currentTarget.value))}
			/>
		</div>
		<output for={id} class="value">{value}</output>
	</div>
	<p class="note rc-caption">
		{#if value === base}
			Written for {base}
		{:else}
			{factorText(value, base)} ·
			<button type="button" class="reset" onclick={reset}>Reset to {base}</button>
		{/if}
	</p>
</section>

<style>
	.servings {
		--iris-a: #d98aa6;
		--iris-b: #a995ee;
		--iris-c: #7cc3e3;
		margin: 34px 0 4px;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 16px;
	}
	label {
		flex: none;
	}
	.slider {
		position: relative;
		flex: 1;
		min-width: 0;
		padding-top: 8px;
	}
	/* The servings the recipe was written for. */
	.base {
		position: absolute;
		top: 2px;
		width: 1px;
		height: 6px;
		margin-left: -1px;
		border-radius: 1px;
		background: var(--rc-muted);
		opacity: 0.6;
	}
	.value {
		flex: none;
		min-width: 1.6ch;
		font: 400 2.4rem / 1.3 var(--rc-serif);
		letter-spacing: -0.04em;
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--rc-ink);
		/* The stretched serif, as on the titles; not feedback. */
		transform: scaleY(1.45);
		transform-origin: center;
		text-shadow: var(--rc-fringe);
	}
	.note {
		min-height: 1.4em;
		margin-top: 2px;
		text-align: right;
	}
	.reset {
		min-height: 0;
		padding: 0;
		border: 0;
		border-radius: 0;
		background: none;
		color: var(--rc-muted);
		font: inherit;
		letter-spacing: inherit;
		text-transform: inherit;
		text-decoration: underline;
		text-underline-offset: 0.2em;
		cursor: pointer;
		transition: color 0.15s ease;
	}
	.reset:hover {
		background: none;
		color: var(--rc-ink);
	}

	/* A fine line and a small bright bead. */
	input[type='range'] {
		-webkit-appearance: none;
		appearance: none;
		display: block;
		width: 100%;
		height: 32px;
		min-height: 0;
		margin: 0;
		padding: 0;
		border: 0;
		background: transparent;
		box-shadow: none;
		cursor: pointer;
	}
	input[type='range']:focus {
		outline: none;
	}
	input[type='range']::-webkit-slider-runnable-track {
		height: 3px;
		border-radius: 999px;
		background: linear-gradient(
			90deg,
			var(--iris-a) 0%,
			var(--iris-b) calc(var(--fill) * 0.6),
			var(--iris-c) var(--fill),
			rgba(255, 255, 255, 0.14) var(--fill)
		);
	}
	input[type='range']::-webkit-slider-thumb {
		-webkit-appearance: none;
		width: 18px;
		height: 18px;
		margin-top: -7.5px;
		border: 0;
		border-radius: 50%;
		background: var(--rc-ink);
		box-shadow:
			0 0 0 4px rgba(0, 0, 0, 0.9),
			0 0 14px rgba(201, 184, 255, 0.55);
		transition: box-shadow 0.15s ease;
	}
	/* Pressed: a brighter glow, never a bigger bead. */
	input[type='range']:active::-webkit-slider-thumb {
		box-shadow:
			0 0 0 4px rgba(0, 0, 0, 0.9),
			0 0 20px rgba(201, 184, 255, 0.85);
	}
	input[type='range']:focus-visible::-webkit-slider-thumb {
		box-shadow:
			0 0 0 4px rgba(0, 0, 0, 0.9),
			0 0 0 5.5px var(--iris-b);
	}
	input[type='range']::-moz-range-track {
		height: 3px;
		border-radius: 999px;
		background: rgba(255, 255, 255, 0.14);
	}
	input[type='range']::-moz-range-progress {
		height: 3px;
		border-radius: 999px;
		background: linear-gradient(90deg, var(--iris-a), var(--iris-b), var(--iris-c));
	}
	input[type='range']::-moz-range-thumb {
		width: 18px;
		height: 18px;
		border: 0;
		border-radius: 50%;
		background: var(--rc-ink);
		box-shadow:
			0 0 0 4px rgba(0, 0, 0, 0.9),
			0 0 14px rgba(201, 184, 255, 0.55);
	}
	input[type='range']:focus-visible::-moz-range-thumb {
		box-shadow:
			0 0 0 4px rgba(0, 0, 0, 0.9),
			0 0 0 5.5px var(--iris-b);
	}

	/* The same small bead in a 44px lane to drag it in, on phones and on touch
	   screens wider than one (an iPad). */
	@media (max-width: 760px), (hover: none) and (pointer: coarse) {
		input[type='range'] {
			height: 44px;
		}
		.reset {
			min-height: 44px;
			margin: -12px 0;
			padding: 0 2px;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.reset,
		input[type='range']::-webkit-slider-thumb {
			transition: none;
		}
	}
</style>
