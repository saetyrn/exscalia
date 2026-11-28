<script lang="ts">
	// The whole recipe as fields, after the recipe app's Review.tsx: the review
	// of a new draft and the edit form are this one form. The page hands it a
	// model, and Save hands the page a valid record (onsave); the page does the
	// call, the cache and the navigation. Nothing here talks to the server.
	import { tick } from 'svelte';
	import Glyph from '$lib/Glyph.svelte';
	import { flip } from '$lib/motion';
	import type { Draft, RecipeIn } from '../types';
	import { UNITS, unitName } from '../units';
	import type { RecipeFormModel } from './form.svelte';
	import { SCALINGS } from './model';

	let {
		model,
		caption,
		method = null,
		missing = [],
		saveLabel,
		busy = false,
		message = '',
		conflict = false,
		onsave,
		ondelete,
		onreload
	}: {
		model: RecipeFormModel;
		/** The small capitals over the title: "Review draft", "Edit recipe". */
		caption: string;
		/** How an import read the draft, and what it could not find. */
		method?: Draft['method'] | null;
		missing?: string[];
		saveLabel: string;
		busy?: boolean;
		/** A failed save, as the server put it. */
		message?: string;
		/** The recipe changed elsewhere since the form loaded. */
		conflict?: boolean;
		onsave: (recipe: RecipeIn) => unknown;
		/** Shows Delete in the save bar. */
		ondelete?: () => unknown;
		/** Offered with the conflict: load the stored recipe again. */
		onreload?: () => unknown;
	} = $props();

	const METHODS: Record<Draft['method'], string> = {
		ai: 'Read by AI',
		schema_org: 'Read from the site’s recipe data',
		local: 'Read without AI'
	};

	let root = $state<HTMLElement>();
	let form = $derived(model.form);
	let errors = $derived(model.errors);

	let sourceLink = $derived.by(() => {
		const url = form.source?.url;
		if (!url) return null;
		try {
			const parsed = new URL(url);
			if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null;
			return { href: parsed.href, host: parsed.hostname.replace(/^www\./, '') };
		} catch {
			return null;
		}
	});

	async function focusIn(selector: string) {
		await tick();
		root?.querySelector<HTMLElement>(selector)?.focus();
	}

	function addIngredient() {
		void focusIn(`[data-ing="${model.addIngredient()}"] .ing-amount`);
	}

	function addStep() {
		void focusIn(`[data-step="${model.addStep()}"] textarea`);
	}

	/** Moving a row re-inserts it; keep the caret on the control that moved
	 * it, or its twin once the row reaches an end. */
	function moveRow(kind: 'ing' | 'step', key: number, by: -1 | 1) {
		if (kind === 'ing') model.moveIngredient(key, by);
		else model.moveStep(key, by);
		const list = kind === 'ing' ? form.ingredients : form.steps;
		const at = list.findIndex((row) => row.key === key);
		const action =
			by < 0 ? (at === 0 ? 'down' : 'up') : at === list.length - 1 ? 'up' : 'down';
		void focusIn(`[data-${kind}="${key}"] [data-action="${action}"]`);
	}

	function removeRow(kind: 'ing' | 'step', key: number) {
		const list = kind === 'ing' ? form.ingredients : form.steps;
		const at = list.findIndex((row) => row.key === key);
		const next = list[at + 1] ?? list[at - 1];
		if (kind === 'ing') model.removeIngredient(key);
		else model.removeStep(key);
		void focusIn(
			next
				? `[data-${kind}="${next.key}"] [data-action="remove"]`
				: `[data-action="add-${kind}"]`
		);
	}

	async function save() {
		if (busy) return;
		const recipe = model.submit();
		if (recipe) await onsave(recipe);
		if (Object.keys(model.errors).length) {
			await tick();
			const field = root?.querySelector<HTMLElement>('[aria-invalid="true"]');
			if (field) field.focus();
			else root?.querySelector('.field-error')?.scrollIntoView({ block: 'center' });
		}
	}

</script>

<div class="rf" bind:this={root}>
	<header class="rf-head">
		<p class="rc-caption">{caption}</p>
		<input
			class="rc-title-input"
			bind:value={form.title}
			placeholder="Title"
			aria-label="Title"
			aria-invalid={!!errors.title}
			aria-describedby={errors.title ? 'rf-title-error' : undefined}
			autocomplete="off"
		/>
		{#if errors.title}<p class="rc-error field-error" id="rf-title-error">{errors.title}</p>{/if}
	</header>

	{#if method || missing.length}
		<aside class="rf-note" aria-label="Import">
			{#if method}<p class="rc-caption">{METHODS[method]}</p>{/if}
			{#if missing.length}
				<ul>
					{#each missing as line, i (i)}<li>{line}</li>{/each}
				</ul>
			{/if}
		</aside>
	{/if}

	<section class="rf-section">
		<div class="rf-row3">
			<label class="rc-field">
				<span>Servings</span>
				<input
					inputmode="numeric"
					bind:value={form.servings}
					aria-invalid={!!errors.servings}
					autocomplete="off"
				/>
				{#if errors.servings}<em class="rc-error field-error">{errors.servings}</em>{/if}
			</label>
			<label class="rc-field">
				<span>Prep min</span>
				<input inputmode="numeric" bind:value={form.prep} aria-invalid={!!errors.prep} autocomplete="off" />
				{#if errors.prep}<em class="rc-error field-error">{errors.prep}</em>{/if}
			</label>
			<label class="rc-field">
				<span>Cook min</span>
				<input inputmode="numeric" bind:value={form.cook} aria-invalid={!!errors.cook} autocomplete="off" />
				{#if errors.cook}<em class="rc-error field-error">{errors.cook}</em>{/if}
			</label>
		</div>
		<label class="rc-field">
			<span>Tags</span>
			<input bind:value={form.tags} aria-invalid={!!errors.tags} autocomplete="off" />
			{#if errors.tags}<em class="rc-error field-error">{errors.tags}</em>{/if}
		</label>
	</section>

	<section class="rf-section" aria-labelledby="rf-ingredients">
		<h2 class="rc-caption" id="rf-ingredients">Ingredients</h2>
		{#if errors.ingredients}<p class="rc-error field-error">{errors.ingredients}</p>{/if}
		{#each form.ingredients as row, i (row.key)}
			<div class="ing-row" data-ing={row.key} animate:flip={{ duration: 220 }}>
				<div class="ing-main">
					<input
						class="ing-amount"
						inputmode="decimal"
						placeholder="amt"
						aria-label="Amount"
						aria-invalid={!!errors[`amount:${row.key}`]}
						bind:value={row.amount}
						autocomplete="off"
					/>
					<select class="ing-unit" aria-label="Unit" bind:value={row.unit}>
						{#each UNITS as unit (unit)}<option value={unit}>{unitName(unit)}</option>{/each}
					</select>
					<input
						class="ing-item"
						placeholder="ingredient"
						aria-label="Ingredient"
						bind:value={row.item}
						autocomplete="off"
					/>
				</div>
				<div class="ing-extra">
					<input class="ing-prep" placeholder="prep" aria-label="Preparation" bind:value={row.prep} autocomplete="off" />
					<input class="ing-group" placeholder="group" aria-label="Group" bind:value={row.group} autocomplete="off" />
					{#if model.ids.get(row.key)}
						<code class="ing-id" title={`Reference in steps: {${model.ids.get(row.key)}}`}>{`{${model.ids.get(row.key)}}`}</code>
					{/if}
					<div class="ing-tools">
						<select class="ing-scaling" aria-label="Scaling" bind:value={row.scaling}>
							{#each SCALINGS as option (option.value)}<option value={option.value}>{option.label}</option>{/each}
						</select>
						<label class="rf-check" title="Amount was estimated">
							<input type="checkbox" bind:checked={row.estimated} />
							<span>estimated</span>
						</label>
						<span class="row-actions">
							<button type="button" class="rc-icon-btn" data-action="up" title="Move up" aria-label="Move up" disabled={i === 0} onclick={() => moveRow('ing', row.key, -1)}>
								<Glyph name="arrow-up" />
							</button>
							<button type="button" class="rc-icon-btn" data-action="down" title="Move down" aria-label="Move down" disabled={i === form.ingredients.length - 1} onclick={() => moveRow('ing', row.key, 1)}>
								<Glyph name="arrow-down" />
							</button>
							<button type="button" class="rc-icon-btn" data-action="remove" title="Remove" aria-label="Remove ingredient" onclick={() => removeRow('ing', row.key)}>
								<Glyph name="x" />
							</button>
						</span>
					</div>
				</div>
				{#if errors[`amount:${row.key}`]}<p class="rc-error field-error">{errors[`amount:${row.key}`]}</p>{/if}
			</div>
		{/each}
		<button type="button" class="rc-link rf-add" data-action="add-ing" onclick={addIngredient}>
			<Glyph name="plus" />Add ingredient
		</button>
	</section>

	<section class="rf-section" aria-labelledby="rf-steps">
		<h2 class="rc-caption" id="rf-steps">Steps</h2>
		{#if errors.steps}<p class="rc-error field-error">{errors.steps}</p>{/if}
		{#each form.steps as step, i (step.key)}
			<div class="step-row" data-step={step.key} animate:flip={{ duration: 220 }}>
				<span class="step-n" aria-hidden="true">{i + 1}</span>
				<textarea rows="2" aria-label={`Step ${i + 1}`} bind:value={step.text}></textarea>
				<div class="step-side">
					<label class="timer-field" title="Timer in minutes">
						<input
							inputmode="decimal"
							aria-label="Timer minutes"
							aria-invalid={!!errors[`timer:${step.key}`]}
							bind:value={step.timer}
							autocomplete="off"
						/>
						<span class="rc-caption">min</span>
					</label>
					<span class="row-actions">
						<button type="button" class="rc-icon-btn" data-action="up" title="Move up" aria-label="Move up" disabled={i === 0} onclick={() => moveRow('step', step.key, -1)}>
							<Glyph name="arrow-up" />
						</button>
						<button type="button" class="rc-icon-btn" data-action="down" title="Move down" aria-label="Move down" disabled={i === form.steps.length - 1} onclick={() => moveRow('step', step.key, 1)}>
							<Glyph name="arrow-down" />
						</button>
						<button type="button" class="rc-icon-btn" data-action="remove" title="Remove" aria-label="Remove step" onclick={() => removeRow('step', step.key)}>
							<Glyph name="x" />
						</button>
					</span>
				</div>
				{#if errors[`timer:${step.key}`]}<p class="rc-error field-error step-error">{errors[`timer:${step.key}`]}</p>{/if}
			</div>
		{/each}
		<button type="button" class="rc-link rf-add" data-action="add-step" onclick={addStep}>
			<Glyph name="plus" />Add step
		</button>
	</section>

	<section class="rf-section">
		<label class="rc-field">
			<span>Notes</span>
			<textarea rows="3" bind:value={form.notes}></textarea>
		</label>
		{#if sourceLink}
			<p class="rc-caption rf-source">
				From <a href={sourceLink.href} target="_blank" rel="noreferrer noopener">{sourceLink.host}</a>
			</p>
		{/if}
	</section>

	{#if errors.form}<p class="rc-error field-error">{errors.form}</p>{/if}
	{#if conflict}
		<p class="rc-error rf-conflict" role="alert">
			Changed elsewhere since you opened it.
			{#if onreload}
				<button type="button" class="rc-link" onclick={() => onreload?.()}>Reload</button>
			{/if}
		</p>
	{:else if message}
		<p class="rc-error" role="alert">{message}</p>
	{/if}

	<div class="rf-savebar">
		{#if ondelete}
			<button type="button" class="rc-link danger" disabled={busy} onclick={() => ondelete?.()}>
				Delete recipe
			</button>
		{/if}
		<span class="rf-spacer"></span>
		<button type="button" class="rc-btn primary rf-save" disabled={busy} onclick={save}>
			{busy ? 'Saving…' : saveLabel}
		</button>
	</div>
</div>

<style>
	.rf-head {
		margin-bottom: 30px;
		text-align: center;
	}
	.rf-head > .rc-caption {
		margin-top: 14px;
	}
	/* The title as a field, in the stretched serif of the page titles. The
	   stretch is layout, not feedback; the margins make room for it. */
	.rf .rc-title-input {
		display: block;
		width: 100%;
		margin: 16px 0 26px;
		padding: 0 0 0.12em;
		border: 0;
		border-bottom: 1px solid transparent;
		border-radius: 0;
		background: none;
		color: var(--rc-ink);
		text-align: center;
		font-family: var(--rc-serif);
		font-size: clamp(2rem, 6.5vw, 3.4rem);
		font-weight: 400;
		line-height: 1.15;
		letter-spacing: -0.05em;
		text-shadow: var(--rc-fringe);
		transform: scaleY(1.6);
	}
	.rf .rc-title-input:hover {
		border-bottom-color: var(--rc-hair);
		background: none;
	}
	.rf .rc-title-input:focus {
		border-bottom-color: rgba(169, 149, 238, 0.6);
		background: none;
	}
	.rf .rc-title-input::placeholder {
		color: var(--rc-muted);
		opacity: 0.5;
	}

	.rf-note {
		margin: 0 0 30px;
		padding: 2px 0 2px 14px;
		border-left: 1.5px solid rgba(169, 149, 238, 0.55);
		color: var(--rc-muted);
		font-size: 0.9rem;
	}
	.rf-note ul {
		margin: 6px 0 0;
		padding-left: 16px;
	}
	.rf-note li + li {
		margin-top: 3px;
	}

	.rf-section {
		padding: 26px 0;
	}
	.rf-section + .rf-section {
		border-top: 1px solid var(--rc-rule);
	}
	.rf-section > h2.rc-caption {
		margin: 0 0 8px;
	}
	.rf-section > .rc-field:first-child {
		margin-top: 0;
	}
	.rf-row3 {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 10px;
	}
	.rf-row3 .rc-field {
		margin-top: 0;
	}
	.field-error {
		margin: 2px 0 0;
		font-size: var(--rc-small);
		font-style: normal;
	}

	.ing-row {
		padding: 10px 0;
	}
	.ing-row,
	.step-row {
		border-bottom: 1px solid var(--rc-rule-faint);
	}
	.ing-main,
	.ing-extra,
	.ing-tools {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	.ing-extra {
		flex-wrap: wrap;
		margin-top: 6px;
	}
	.rf .ing-extra input:not([type='checkbox']),
	.rf .ing-extra select {
		min-height: 34px;
		padding: 4px 9px;
		font-size: 0.88rem;
	}
	.ing-amount {
		flex: none;
		width: 74px;
	}
	.ing-unit {
		flex: none;
		width: 94px;
	}
	.ing-item {
		flex: 1;
		min-width: 0;
	}
	.ing-prep {
		flex: 1 1 110px;
		min-width: 0;
	}
	.ing-group {
		flex: 1 1 90px;
		min-width: 0;
	}
	.ing-id {
		flex: none;
		max-width: 9em;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--rc-muted);
		font: 400 0.74rem/1 var(--rc-mono);
	}
	.ing-tools {
		flex: none;
	}
	.ing-scaling {
		width: 142px;
	}
	.rf-check {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		color: var(--rc-muted);
		font-size: 0.72rem;
		letter-spacing: 0.04em;
		cursor: pointer;
	}
	.rf-check input {
		margin: 0;
		accent-color: var(--rc-ink);
	}
	.row-actions {
		display: inline-flex;
		margin-left: auto;
	}
	.rc-icon-btn:disabled {
		opacity: 0.25;
		cursor: default;
	}

	.step-row {
		display: grid;
		grid-template-columns: 26px minmax(0, 1fr) auto;
		gap: 8px;
		align-items: start;
		padding: 8px 0;
	}
	.step-n {
		padding-top: 10px;
		color: var(--rc-muted);
		text-align: right;
		font: italic 400 1.1rem/1 var(--rc-serif);
	}
	.step-side {
		display: flex;
		flex-direction: column;
		align-items: flex-end;
		gap: 4px;
	}
	.step-error {
		grid-column: 2 / -1;
	}
	.timer-field {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 0.82rem;
	}
	.timer-field .rc-caption {
		letter-spacing: 0.1em;
	}
	.rf .timer-field input {
		width: 66px;
		min-height: 34px;
		padding: 4px 9px;
	}

	.rf-add {
		margin-top: 14px;
	}
	.rf-source {
		margin-top: 6px;
	}
	.rf-source a {
		color: var(--rc-ink);
	}
	.rf-conflict {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 12px;
	}

	/* The page fades to black under the save bar instead of a box. */
	.rf-savebar {
		position: sticky;
		bottom: 0;
		z-index: 6;
		display: flex;
		align-items: center;
		gap: 12px;
		margin-top: 8px;
		padding: 34px 0 calc(18px + env(safe-area-inset-bottom));
		background: linear-gradient(to top, var(--rc-bg) 58%, rgba(0, 0, 0, 0));
	}
	.rf-spacer {
		flex: 1;
	}
	.rf-save {
		flex: 1;
		min-height: 46px;
	}

	/* A touch screen wider than a phone (an iPad): the phone's 44px targets,
	   in the wide layout. */
	@media (hover: none) and (pointer: coarse) {
		.rf :is(input:not([type='checkbox']), select),
		.rf .ing-extra :is(input:not([type='checkbox']), select),
		.rf .timer-field input {
			min-height: 44px;
		}
		.rf-check {
			min-height: 44px;
			padding: 0 4px;
		}
		.rf-check input {
			width: 20px;
			height: 20px;
		}
		/* The bigger row buttons take a line of their own, as on phones. */
		.ing-extra {
			flex-wrap: wrap;
		}
		.ing-tools {
			flex: 1 1 100%;
			min-width: 0;
		}
	}

	@media (max-width: 760px) {
		/* Every field a thumb can hit: 44px tall. */
		.rf :is(input:not([type='checkbox']), select) {
			min-height: 44px;
		}
		.rf-note {
			font-size: var(--m-meta);
		}
		.rf-row3 {
			gap: 6px;
		}
		.ing-main {
			gap: 6px;
		}
		.rf .ing-extra input:not([type='checkbox']),
		.rf .ing-extra select {
			min-height: 44px;
			font-size: var(--m-row-text);
		}
		.ing-prep,
		.ing-group {
			flex: 1 1 0;
		}
		.ing-tools {
			flex: 1 1 100%;
			min-width: 0;
			gap: 4px;
		}
		.ing-scaling {
			flex: 1 1 0;
			width: auto;
			min-width: 0;
		}
		.rf-check {
			min-height: 44px;
			padding: 0 4px;
			font-size: var(--m-label);
		}
		.rf-check input {
			width: 20px;
			height: 20px;
		}
		.step-row {
			grid-template-columns: 22px minmax(0, 1fr);
		}
		.step-side {
			grid-column: 2;
			flex-direction: row;
			justify-content: space-between;
			align-items: center;
		}
		.rf .timer-field input {
			width: 72px;
			min-height: 44px;
		}
		.rf-source a {
			display: inline-flex;
			align-items: center;
			min-height: 44px;
		}
		/* Save takes the rest of the row, so "Save changes" never wraps
		   beside Delete on a narrow phone. */
		.rf-spacer {
			flex: 0 0 4px;
		}
	}
</style>
