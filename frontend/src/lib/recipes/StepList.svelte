<script lang="ts">
	// The method, after the recipe app's StepList: numbered in the serif, each
	// {id} reference shown as the scaled amount and item, and a step's timer as
	// a caption under it. Timers are stored and shown; they do not run.
	import { formatTimer } from './format';
	import { renderStep } from './scaling';
	import type { Ingredient, Step } from './types';

	let {
		steps,
		ingredients,
		factor
	}: {
		steps: Step[];
		ingredients: Ingredient[];
		factor: number;
	} = $props();
</script>

{#if steps.length > 0}
	<section class="steps">
		<h2 class="rc-caption">Steps</h2>
		<ol>
			{#each steps as step}
				<li>
					<!-- One line on purpose: the segments run together as text. -->
					<p>{#each renderStep(step.text, ingredients, factor) as seg}{#if seg.kind === 'ref'}<span class="ref" title={seg.scaled.markers.includes('estimated') ? 'Estimated amount' : undefined}>{seg.text}</span>{:else if seg.kind === 'missing'}<span class="ref missing" title="No matching ingredient">{seg.text}</span>{:else}{seg.text}{/if}{/each}</p>
					{#if step.timer_seconds}
						<span class="timer rc-caption">{formatTimer(step.timer_seconds)}</span>
					{/if}
				</li>
			{/each}
		</ol>
	</section>
{/if}

<style>
	ol {
		margin: 0;
		padding: 0;
		list-style: none;
		counter-reset: step;
	}
	li {
		position: relative;
		padding: 12px 0 12px 36px;
		border-bottom: 1px solid var(--rc-rule);
	}
	li:last-child {
		border-bottom: 0;
	}
	/* Serif numerals, no circles, to match the bare icons. */
	li::before {
		counter-increment: step;
		content: counter(step);
		position: absolute;
		top: 11px;
		left: 0;
		width: 22px;
		text-align: right;
		font: italic 400 1.2rem / 1.3 var(--rc-serif);
		color: var(--rc-muted);
	}
	p {
		font-size: 1.05rem;
		line-height: 1.6;
		overflow-wrap: anywhere;
		white-space: pre-line;
	}
	.ref {
		font-weight: 600;
		border-bottom: 1px dotted var(--rc-muted);
	}
	.ref.missing {
		color: var(--rc-danger);
		border-bottom-color: var(--rc-danger);
	}
	.timer {
		display: inline-block;
		margin-top: 5px;
		font-size: 0.66rem;
	}

	@media (max-width: 760px) {
		p {
			font-size: var(--rc-text);
		}
		.timer {
			font-size: var(--rc-label);
		}
	}
</style>
