// The recipe form's state and its mapping to and from RecipeIn, after the
// recipe app's Review.tsx. Numbers are held as the text in their inputs, so a
// half-typed amount is the user's until Save, and every loaded number keeps
// its exact value beside its text: a recipe saved without touching "0.333"
// still stores 1/3.
//
// Ingredient ids: steps name ingredients as {id}, so an id never changes once
// it exists. Renaming an item keeps it; a new row gets one from its item when
// the form is saved; a repeated id gets -2, -3 as the server's AI step does.
// Removing an ingredient leaves the steps as they are, as the recipe app did:
// the recipe page marks a reference that no longer matches.
import type { Draft, Ingredient, RecipeIn, Scaling, Source, Unit } from '../types';
import { emptyRecipe } from '../types';

export interface FormIngredient {
	/** Client key for the keyed list; never sent. */
	key: number;
	/** '' until the form is saved: then derived from the item. */
	id: string;
	amount: string;
	/** The loaded amount; used when `amount` is still its text. */
	amountValue: number | null;
	unit: Unit;
	item: string;
	prep: string;
	scaling: Scaling;
	estimated: boolean;
	group: string;
}

export interface FormStep {
	key: number;
	text: string;
	/** Minutes, as typed. */
	timer: string;
	/** The loaded timer in seconds; used when `timer` is still its text. */
	timerValue: number | null;
}

export interface FormState {
	title: string;
	servings: string;
	prep: string;
	cook: string;
	/** Comma-separated, as typed. */
	tags: string;
	ingredients: FormIngredient[];
	steps: FormStep[];
	notes: string;
	/** Carried through untouched: type, link and card thumbnail. */
	source: Source | null;
}

/** Field messages, keyed by field: `title`, `servings`, `prep`, `cook`,
 * `tags`, `ingredients`, `steps`, `amount:<key>`, `timer:<key>`, and `form`
 * for anything that belongs to no field. */
export type FieldErrors = Record<string, string>;

export const SCALINGS: { value: Scaling; label: string }[] = [
	{ value: 'linear', label: 'linear' },
	{ value: 'whole', label: 'whole numbers' },
	{ value: 'to_taste', label: 'to taste' }
];

export const MAX_TITLE = 200;
export const MAX_SERVINGS = 100;
/** Ingredient ids may be 64 characters; a base this long leaves room for "-12". */
const MAX_ID_BASE = 48;

let nextKey = 1;
export const newKey = (): number => nextKey++;

/** An id from an item's words: the first three, lowercased, accents dropped. */
export function slug(text: string): string {
	const s = text
		.normalize('NFKD')
		.replace(/[̀-ͯ]/g, '')
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '-')
		.replace(/^-|-$/g, '');
	const base = s.split('-').slice(0, 3).join('-').slice(0, MAX_ID_BASE).replace(/-$/, '');
	return base || 'item';
}

/** `base`, or `base-2`, `base-3`… whichever is free; the result is taken. */
function claim(base: string, taken: Set<string>): string {
	let id = base;
	for (let n = 2; taken.has(id); n++) id = `${base}-${n}`;
	taken.add(id);
	return id;
}

/** The id each row will be saved with, by key: rows that have one keep it
 * (a repeat of an earlier row's gets a suffix), and new rows take one from
 * their item that no other row holds. Rows with no item have none. */
export function assignIds(rows: readonly FormIngredient[]): Map<number, string> {
	const out = new Map<number, string>();
	const taken = new Set<string>();
	const named = rows.filter((row) => row.item.trim());
	for (const row of named) if (row.id) out.set(row.key, claim(row.id, taken));
	for (const row of named) if (!row.id) out.set(row.key, claim(slug(row.item), taken));
	return out;
}

const FRACTIONS: Record<string, number> = {
	'¼': 1 / 4,
	'½': 1 / 2,
	'¾': 3 / 4,
	'⅓': 1 / 3,
	'⅔': 2 / 3,
	'⅛': 1 / 8,
	'⅜': 3 / 8,
	'⅝': 5 / 8,
	'⅞': 7 / 8
};

/** An amount as typed: "2", "2.5", "2,5", "1/2", "1 1/2", "1½". Null when
 * blank, NaN when it is not an amount. */
export function parseAmount(text: string): number | null {
	const t = text.trim();
	if (!t) return null;
	const vulgar = /^(\d+)?\s*([¼½¾⅓⅔⅛⅜⅝⅞])$/.exec(t);
	if (vulgar) return Number(vulgar[1] ?? 0) + FRACTIONS[vulgar[2]];
	const mixed = /^(?:(\d+)\s+)?(\d+)\s*\/\s*(\d+)$/.exec(t);
	if (mixed) {
		const den = Number(mixed[3]);
		return den ? Number(mixed[1] ?? 0) + Number(mixed[2]) / den : NaN;
	}
	if (/^\d*[.,]?\d+$|^\d+[.,]$/.test(t)) return Number(t.replace(',', '.'));
	return NaN;
}

/** A stored amount as the input shows it. */
export function formatAmount(value: number | null): string {
	if (value === null || !Number.isFinite(value)) return '';
	return String(Math.round(value * 1000) / 1000);
}

function formatTimer(seconds: number | null): string {
	return seconds ? String(Math.round((seconds / 60) * 100) / 100) : '';
}

/** Tags as typed into one field: split on commas, lowercased, deduped. */
export function parseTags(text: string): string[] {
	const seen = new Set<string>();
	for (const raw of text.split(',')) {
		const tag = raw.trim().toLowerCase();
		if (tag) seen.add(tag);
	}
	return [...seen];
}

/** Move one entry; out-of-range targets leave the list as it is. */
export function move<T>(list: readonly T[], from: number, to: number): T[] {
	if (from < 0 || from >= list.length || to < 0 || to >= list.length) return [...list];
	const next = [...list];
	const [item] = next.splice(from, 1);
	next.splice(to, 0, item);
	return next;
}

export function blankIngredient(): FormIngredient {
	return {
		key: newKey(),
		id: '',
		amount: '',
		amountValue: null,
		unit: '',
		item: '',
		prep: '',
		scaling: 'linear',
		estimated: false,
		group: ''
	};
}

export function blankStep(): FormStep {
	return { key: newKey(), text: '', timer: '', timerValue: null };
}

/** The form for a recipe, a draft's recipe, or a fresh one. Forgiving: a
 * stored draft from an older build may lack fields. */
export function fromRecipe(recipe: Partial<RecipeIn> = emptyRecipe()): FormState {
	const base = { ...emptyRecipe(), ...recipe };
	return {
		title: String(base.title ?? ''),
		servings: String(base.base_servings ?? 2),
		prep: base.prep_minutes == null ? '' : String(base.prep_minutes),
		cook: base.cook_minutes == null ? '' : String(base.cook_minutes),
		tags: (base.tags ?? []).join(', '),
		ingredients: (base.ingredients ?? []).map((i) => {
			const amount = typeof i.amount === 'number' ? i.amount : null;
			return {
				key: newKey(),
				id: i.id ?? '',
				amount: formatAmount(amount),
				amountValue: amount,
				unit: i.unit ?? '',
				item: i.item ?? '',
				prep: i.prep ?? '',
				scaling: i.scaling ?? 'linear',
				estimated: !!i.estimated,
				group: i.group ?? ''
			};
		}),
		steps: (base.steps ?? []).map((s) => {
			const seconds = typeof s.timer_seconds === 'number' ? s.timer_seconds : null;
			return { key: newKey(), text: s.text ?? '', timer: formatTimer(seconds), timerValue: seconds };
		}),
		notes: String(base.notes ?? ''),
		source: base.source ?? null
	};
}

/** The form for an import's draft. */
export function fromDraft(draft: Draft | null): FormState {
	return fromRecipe(draft?.recipe);
}

/** A form for typing a recipe in from nothing: one empty row of each. */
export function emptyForm(): FormState {
	const form = fromRecipe();
	form.ingredients = [blankIngredient()];
	form.steps = [blankStep()];
	return form;
}

const WHOLE = /^\d+$/;

/** The form as a clean record, blank rows dropped, ids assigned, plus a
 * message per field the server would refuse. The record leaves the art out,
 * so an update keeps the stored drawing. */
export function toRecipeIn(form: FormState): { recipe: RecipeIn; errors: FieldErrors } {
	const errors: FieldErrors = {};
	const title = form.title.trim();
	if (!title) errors.title = 'Title required';
	else if (title.length > MAX_TITLE) errors.title = `Up to ${MAX_TITLE} characters`;

	const servingsText = form.servings.trim();
	const servings = WHOLE.test(servingsText) ? Number(servingsText) : NaN;
	if (!(servings >= 1 && servings <= MAX_SERVINGS)) errors.servings = `1 to ${MAX_SERVINGS}`;

	const minutes = (text: string, field: string): number | null => {
		const t = text.trim();
		if (!t) return null;
		if (WHOLE.test(t)) return Number(t);
		errors[field] = 'Whole minutes';
		return null;
	};
	const prep = minutes(form.prep, 'prep');
	const cook = minutes(form.cook, 'cook');

	const ids = assignIds(form.ingredients);
	const ingredients: Ingredient[] = [];
	for (const row of form.ingredients) {
		const item = row.item.trim();
		if (!item) continue;
		let amount: number | null;
		if (row.amount === formatAmount(row.amountValue)) amount = row.amountValue;
		else {
			amount = parseAmount(row.amount);
			if (amount !== null && !(amount >= 0)) {
				errors[`amount:${row.key}`] = 'Not an amount';
				amount = null;
			}
		}
		ingredients.push({
			id: ids.get(row.key) ?? slug(item),
			amount,
			unit: row.unit,
			item,
			prep: row.prep.trim(),
			scaling: row.scaling,
			estimated: row.estimated,
			group: row.group.trim() || null
		});
	}

	const steps = [];
	for (const step of form.steps) {
		const text = step.text.trim();
		if (!text) continue;
		let timer: number | null;
		if (step.timer === formatTimer(step.timerValue)) timer = step.timerValue;
		else {
			const mins = parseAmount(step.timer);
			if (mins !== null && !(mins >= 0)) {
				errors[`timer:${step.key}`] = 'Not a number';
				timer = null;
			} else {
				const seconds = mins === null ? 0 : Math.round(mins * 60);
				timer = seconds >= 1 ? seconds : null;
			}
		}
		steps.push({ text, timer_seconds: timer });
	}

	return {
		recipe: {
			title,
			base_servings: Number.isFinite(servings) ? servings : 2,
			prep_minutes: prep,
			cook_minutes: cook,
			ingredients,
			steps,
			notes: form.notes.trim(),
			tags: parseTags(form.tags),
			source: form.source
		},
		errors
	};
}

// The server's model fields, by the form field that shows their message.
const SERVER_FIELDS: Record<string, string> = {
	title: 'title',
	base_servings: 'servings',
	prep_minutes: 'prep',
	cook_minutes: 'cook',
	tags: 'tags',
	notes: 'form',
	id: 'ingredients',
	amount: 'ingredients',
	unit: 'ingredients',
	item: 'ingredients',
	prep: 'ingredients',
	scaling: 'ingredients',
	estimated: 'ingredients',
	group: 'ingredients',
	text: 'steps',
	timer_seconds: 'steps'
};

/** A 422's detail ("title: String should have at least 1 character; ...",
 * as main.py flattens it) as field messages. The first message per field
 * wins; a part that names no known field goes to `form`. */
export function serverErrors(detail: string): FieldErrors {
	const errors: FieldErrors = {};
	for (const part of detail.split('; ')) {
		const m = /^([a-z_]+): (.+)$/.exec(part.trim());
		const field = (m && SERVER_FIELDS[m[1]]) || 'form';
		const message = m && SERVER_FIELDS[m[1]] ? m[2] : part.trim();
		if (message && !errors[field]) errors[field] = message;
	}
	return errors;
}
