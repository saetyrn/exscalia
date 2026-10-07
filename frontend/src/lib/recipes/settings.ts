// The Recipes effort select on the Settings page. Unset is not the chat knob,
// as it is for the other roles: recipe work has defaults of its own, so the
// unset option reads "Auto", and the levels in effect are its tooltip.
import type { OmniaSettings } from '$lib/api';

type RecipesEffort = Pick<
	OmniaSettings,
	'reasoning_levels' | 'recipes_reasoning' | 'recipes_reasoning_in_effect'
>;

/** The select's value: the owner's level, or "inherit" when none is set. */
export function recipesEffortValue(settings: RecipesEffort): string {
	// The current server says so outright. An older one sent the level in
	// effect in `recipes_reasoning`, and only the override list tells.
	if (settings.recipes_reasoning_in_effect) return settings.recipes_reasoning ?? 'inherit';
	return settings.reasoning_levels?.recipes ?? 'inherit';
}

const label = (level: string) => level.charAt(0).toUpperCase() + level.slice(1);

/** What each job runs at, for the tooltip; '' when the server does not say. */
export function recipesEffortTitle(settings: RecipesEffort): string {
	const effect = settings.recipes_reasoning_in_effect;
	if (!effect) return '';
	return `Import: ${label(effect.import)} · Drawing: ${label(effect.drawing)}`;
}
