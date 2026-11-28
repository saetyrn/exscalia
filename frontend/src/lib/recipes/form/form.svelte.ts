// One recipe form's live state: the fields (bound straight to the inputs),
// the row actions, and the messages a save attempt leaves. Shared by the
// review of a new draft and the edit form; RecipeForm.svelte draws it.
import {
	assignIds,
	blankIngredient,
	blankStep,
	move,
	serverErrors,
	toRecipeIn,
	type FieldErrors,
	type FormState
} from './model';
import type { RecipeIn } from '../types';

export class RecipeFormModel {
	form: FormState = $state() as FormState;
	/** Messages from the last 422, kept until the next save attempt. */
	serverErrors: FieldErrors = $state({});
	/** A save was tried: from then on the client's checks show as you type. */
	attempted = $state(false);
	/** What the form held when it was loaded, to tell an untouched form. */
	#loaded = '';

	constructor(form: FormState) {
		this.load(form);
	}

	/** Replace everything: a fresh load, or a reload after a conflict. */
	load(form: FormState): void {
		this.form = form;
		this.serverErrors = {};
		this.attempted = false;
		this.#loaded = JSON.stringify(form);
	}

	/** Nothing has been changed since the last load. */
	get pristine(): boolean {
		return JSON.stringify(this.form) === this.#loaded;
	}

	/** The id each ingredient row will be saved with, by row key. */
	ids: Map<number, string> = $derived(assignIds(this.form.ingredients));

	/** What to show now: the client's checks once a save was tried, then the
	 * server's. */
	errors: FieldErrors = $derived({
		...this.serverErrors,
		...(this.attempted ? toRecipeIn(this.form).errors : {})
	});

	addIngredient(): number {
		const row = blankIngredient();
		this.form.ingredients.push(row);
		return row.key;
	}

	removeIngredient(key: number): void {
		this.form.ingredients = this.form.ingredients.filter((row) => row.key !== key);
	}

	moveIngredient(key: number, by: -1 | 1): void {
		const at = this.form.ingredients.findIndex((row) => row.key === key);
		this.form.ingredients = move(this.form.ingredients, at, at + by);
	}

	addStep(): number {
		const step = blankStep();
		this.form.steps.push(step);
		return step.key;
	}

	removeStep(key: number): void {
		this.form.steps = this.form.steps.filter((step) => step.key !== key);
	}

	moveStep(key: number, by: -1 | 1): void {
		const at = this.form.steps.findIndex((step) => step.key === key);
		this.form.steps = move(this.form.steps, at, at + by);
	}

	/** The record to save, or null with the messages showing. */
	submit(): RecipeIn | null {
		this.attempted = true;
		this.serverErrors = {};
		const { recipe, errors } = toRecipeIn(this.form);
		return Object.keys(errors).length ? null : recipe;
	}

	/** The server refused the record (422): show its detail by field. */
	refused(detail: string): void {
		this.serverErrors = serverErrors(detail);
	}
}
