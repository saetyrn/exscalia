// Export as PDF: recipes laid out for paper, then the system print dialog,
// whose "Save as PDF" makes the file. The sheets are built into a container
// appended to <body>; while it is there, `rc-printing` on the root hides the
// whole app from print (recipes.css), so the panes' scroll boxes cannot cut a
// recipe off at the first page. On screen the container never shows.
//
// The Mac desktop shell's WebKit ignores window.print(), so there an export
// opens the same page in the system browser with ?print, which prints there.
import { apiBase, desktopPlatform } from '$lib/runtime';
import { renderMarkdown } from '$lib/render';
import { formatTimer } from './format';
import { RECIPES_HOME, recipeHref } from './paths';
import { groupIngredients, metaLine, sourceLink } from './recipepage';
import { renderStep, scaleIngredient } from './scaling';
import type { Recipe } from './types';
import { itemLabel } from './units';

export interface PrintItem {
	recipe: Recipe;
	servings: number;
}

const MARKER_WORD = { estimated: 'estimated', rounded: 'rounded', to_taste: 'starting point' } as const;

function el<K extends keyof HTMLElementTagNameMap>(
	doc: Document,
	tag: K,
	className?: string,
	text?: string
): HTMLElementTagNameMap[K] {
	const node = doc.createElement(tag);
	if (className) node.className = className;
	if (text !== undefined) node.textContent = text;
	return node;
}

/** One recipe as a printed page: drawing, title, time and tags, the
 * servings, ingredients and steps scaled to them, notes, and the source. */
export function recipeSheet(item: PrintItem, doc: Document = document): HTMLElement {
	const { recipe, servings } = item;
	const factor = servings / recipe.base_servings;
	const sheet = el(doc, 'article', 'rc-print-sheet');

	if (recipe.art.trim()) sheet.append(el(doc, 'pre', 'rc-print-art', recipe.art));
	sheet.append(el(doc, 'h1', 'rc-print-title', recipe.title));
	const meta = metaLine(recipe);
	const line = [meta, `Serves ${servings}`].filter(Boolean).join(' · ');
	sheet.append(el(doc, 'p', 'rc-print-meta', line));

	if (recipe.ingredients.length) {
		const section = el(doc, 'section', 'rc-print-ingredients');
		section.append(el(doc, 'h2', undefined, 'Ingredients'));
		for (const group of groupIngredients(recipe.ingredients)) {
			if (group.name) section.append(el(doc, 'h3', undefined, group.name));
			const list = el(doc, 'ul');
			for (const ing of group.items) {
				const scaled = scaleIngredient(ing, factor);
				const li = el(doc, 'li');
				li.append(
					el(doc, 'span', 'rc-print-amt', scaled.text || (ing.scaling === 'to_taste' ? 'to taste' : ''))
				);
				const what = el(doc, 'span', 'rc-print-item', itemLabel(ing.item, scaled.amount, scaled.unit));
				if (ing.prep) what.append(el(doc, 'span', 'rc-print-muted', `, ${ing.prep}`));
				const words = scaled.markers.map((m) => MARKER_WORD[m]);
				if (words.length) what.append(el(doc, 'span', 'rc-print-marker', ` ${words.join(', ')}`));
				li.append(what);
				list.append(li);
			}
			section.append(list);
		}
		sheet.append(section);
	}

	if (recipe.steps.length) {
		const section = el(doc, 'section', 'rc-print-steps');
		section.append(el(doc, 'h2', undefined, 'Steps'));
		const list = el(doc, 'ol');
		for (const step of recipe.steps) {
			const li = el(doc, 'li');
			const p = el(doc, 'p');
			for (const seg of renderStep(step.text, recipe.ingredients, factor)) {
				p.append(seg.kind === 'text' ? doc.createTextNode(seg.text) : el(doc, 'strong', undefined, seg.text));
			}
			li.append(p);
			if (step.timer_seconds) li.append(el(doc, 'span', 'rc-print-timer', formatTimer(step.timer_seconds)));
			list.append(li);
		}
		section.append(list);
		sheet.append(section);
	}

	if (recipe.notes.trim()) {
		const section = el(doc, 'section', 'rc-print-notes');
		section.append(el(doc, 'h2', undefined, 'Notes'));
		const body = el(doc, 'div');
		// renderMarkdown sanitizes: raw HTML shows as text, as on the page.
		body.innerHTML = renderMarkdown(recipe.notes, { chat: true, headingOffset: 2 });
		section.append(body);
		sheet.append(section);
	}

	const source = sourceLink(recipe.source?.url);
	if (source) sheet.append(el(doc, 'p', 'rc-print-source', `From ${source.href}`));
	return sheet;
}

let cleanup: (() => void) | null = null;

/** Lay the recipes out for paper and open the print dialog. `title` names
 * the PDF: browsers offer the document title as its file name. */
export function printRecipes(items: PrintItem[], title: string): void {
	if (!items.length) return;
	cleanup?.();
	const root = document.documentElement;
	const box = el(document, 'div', 'rc-print');
	box.setAttribute('aria-hidden', 'true');
	for (const item of items) box.append(recipeSheet(item));
	document.body.append(box);
	root.classList.add('rc-printing');
	const before = document.title;
	document.title = title;

	const done = () => {
		window.removeEventListener('afterprint', done);
		box.remove();
		root.classList.remove('rc-printing');
		if (document.title === title) document.title = before;
		if (cleanup === done) cleanup = null;
	};
	cleanup = done;
	window.addEventListener('afterprint', done);
	window.print();
}

/** Whether print() works here. The Mac desktop shell's WKWebView drops it. */
export function canPrintHere(): boolean {
	return desktopPlatform !== 'mac';
}

/** The page that prints itself on arrival: a recipe at a serving count, or
 * every recipe ("all"), on the server the app talks to. */
export function printPageUrl(path: string | null, servings?: number): string {
	const page = path ? recipeHref(path) : RECIPES_HOME;
	const query = path ? String(servings ?? '') : 'all';
	return `${apiBase() || window.location.origin}${page}?print=${encodeURIComponent(query)}`;
}

/** Hand a URL to the system browser: the desktop opener acts on a
 * target=_blank click. */
export function openOutside(url: string): void {
	const a = document.createElement('a');
	a.href = url;
	a.target = '_blank';
	a.rel = 'noopener noreferrer';
	document.body.append(a);
	a.click();
	a.remove();
}

/** The ?print value a page arrived with: a serving count, "all", or null. */
export function printRequest(url: URL): number | 'all' | null {
	const raw = url.searchParams.get('print');
	if (raw === null) return null;
	if (raw === 'all') return 'all';
	const n = Number(raw);
	return Number.isInteger(n) && n > 0 ? n : 0;
}

/** File name for a print of many recipes. */
export function collectionTitle(count: number): string {
	return count === 1 ? 'Recipe' : `Recipes (${count})`;
}
