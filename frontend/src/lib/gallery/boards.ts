// Making a board, shared by the Gallery pages.
import { api } from '$lib/api';
import { notify, promptDialog } from '$lib/ui.svelte';

export async function newBoard(): Promise<string | null> {
	const name = await promptDialog('', {
		title: 'New board',
		placeholder: 'Name',
		confirmLabel: 'Create'
	});
	if (!name?.trim()) return null;
	try {
		return (await api.galleryCreateBoard(name.trim())).name;
	} catch (e) {
		notify((e as Error).message, { tone: 'error' });
		return null;
	}
}
