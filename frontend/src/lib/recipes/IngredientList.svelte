<script lang="ts">
	// The ingredients at the chosen servings, after the recipe app's
	// IngredientList: amount, item (counted items follow their number), prep,
	// then the markers. Grouped under their group names when a recipe has
	// groups. Every amount is scaled from the stored one by `factor`.
	import Markers from './Markers.svelte';
	import { groupIngredients } from './recipepage';
	import { scaleIngredient } from './scaling';
	import type { Ingredient } from './types';
	import { itemLabel } from './units';

	let {
		ingredients,
		factor,
		onconfirm,
		busy = false
	}: {
		ingredients: Ingredient[];
		factor: number;
		/** Confirm an estimated amount; without it "estimated" is only a word. */
		onconfirm?: (id: string) => void;
		busy?: boolean;
	} = $props();

	let groups = $derived(groupIngredients(ingredients));
</script>

<section class="ingredients">
	<h2 class="rc-caption">Ingredients</h2>
	{#each groups as group}
		{#if group.name}<h3>{group.name}</h3>{/if}
		<ul>
			{#each group.items as ing}
				{@const scaled = scaleIngredient(ing, factor)}
				<li>
					<span class="amt">{scaled.text || (ing.scaling === 'to_taste' ? 'to taste' : '')}</span>
					<span class="item"
						>{itemLabel(ing.item, scaled.amount, scaled.unit)}{#if ing.prep}<span class="rc-muted"
								>, {ing.prep}</span
							>{/if}</span
					>
					<Markers
						markers={scaled.markers}
						{busy}
						onconfirm={onconfirm ? () => onconfirm(ing.id) : undefined}
					/>
				</li>
			{/each}
		</ul>
	{/each}
</section>

<style>
	h3 {
		margin: 1rem 0 0.3rem;
		font: 600 0.85rem / 1.3 var(--rc-sans);
		letter-spacing: 0.02em;
		color: var(--rc-muted);
	}
	ul {
		margin: 0;
		padding: 0;
		list-style: none;
	}
	li {
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		gap: 0 12px;
		padding: 9px 2px;
		border-bottom: 1px solid var(--rc-rule);
		font-size: var(--rc-text);
	}
	li:last-child {
		border-bottom: 0;
	}
	.amt {
		flex: 0 0 88px;
		font-weight: 600;
		letter-spacing: -0.01em;
		font-variant-numeric: tabular-nums;
	}
	.item {
		flex: 1 1 160px;
		min-width: 0;
		overflow-wrap: anywhere;
	}
</style>
