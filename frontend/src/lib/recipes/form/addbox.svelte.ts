// The add box's state, after the recipe app's Add.tsx: typed text or a link,
// and up to six photos or PDFs, read into a draft by the import routes. The
// server decides how (its local parser, the site's recipe data, or the AI
// step) and answers a failure with a short detail, shown as it is.
import { api } from '$lib/api';
import type { Draft } from '../types';
import { isUpload, MAX_FILES, pickFiles, prepareUpload, type Upload } from './files';
import { setDraft } from './handoff.svelte';

/** A box that holds one link and nothing else is a link import. */
const LINK = /^https?:\/\/\S+$/i;

export class AddBox {
	text = $state('');
	uploads: Upload[] = $state([]);
	busy = $state(false);
	error = $state('');
	/** Whether the AI step can read photos and PDFs; null until asked. */
	ai: boolean | null = $state(null);
	#status: Promise<void> | null = null;
	#key = 0;

	ready = $derived(!this.busy && (this.text.trim().length > 0 || this.uploads.length > 0));
	full = $derived(this.uploads.length >= MAX_FILES);

	/** Ask the server once whether photos and PDFs can be read. */
	loadStatus(): Promise<void> {
		this.#status ??= api.recipesStatus().then(
			(status) => void (this.ai = status.ai === true),
			() => void (this.ai = false)
		);
		return this.#status;
	}

	/** Take picked, dropped or pasted files: photos and PDFs, up to six. */
	async takeFiles(list: readonly File[]): Promise<void> {
		if (!list.length) return;
		await this.loadStatus();
		if (!this.ai) {
			this.error = list.some(isUpload)
				? 'Photos and PDFs need an AI provider.'
				: 'Only photos and PDFs can be read.';
			return;
		}
		const { take, error } = pickFiles(list, this.uploads.length);
		this.error = error;
		if (!take.length) return;
		try {
			const added = await Promise.all(take.map((file) => prepareUpload(file, ++this.#key)));
			this.uploads = [...this.uploads, ...added].slice(0, MAX_FILES);
		} catch (cause) {
			this.error = (cause as Error).message;
		}
	}

	remove(key: number): void {
		this.uploads = this.uploads.filter((upload) => upload.key !== key);
	}

	/** Dropped text joins what is there on a new line. */
	appendText(text: string): void {
		this.text = this.text ? `${this.text}\n${text}` : text;
	}

	/** Read the box into a draft and hand it to the review page. Null when
	 * the import failed; `error` then holds the server's detail. */
	async submit(): Promise<Draft | null> {
		if (!this.ready) return null;
		this.busy = true;
		this.error = '';
		try {
			const text = this.text.trim();
			let draft: Draft;
			let thumb: string | null = null;
			if (this.uploads.length) {
				draft = await api.recipesImportFiles(
					this.uploads.map((upload) => upload.file),
					text || undefined
				);
				thumb = this.uploads.find((upload) => upload.thumb)?.thumb ?? null;
			} else if (LINK.test(text)) {
				draft = await api.recipesImport({ url: text });
			} else {
				draft = await api.recipesImport({ text });
			}
			setDraft(draft, thumb);
			return draft;
		} catch (cause) {
			this.error = (cause as Error).message || 'The recipe could not be read.';
			return null;
		} finally {
			this.busy = false;
		}
	}
}
