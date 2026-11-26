<script lang="ts">
	// OMNIA, one tap from the note you are reading.
	//
	// The note page already carries a per-note ChatPanel — inside the right
	// panel's OMNIA tab. On a laptop that panel is a column beside the editor;
	// on a phone the same panel stacks *below* a 100dvh editor, so asking a
	// question about the note meant scrolling past the entire note first. This
	// is the same conversation, the same thread, in a drawer.
	//
	// Nothing here duplicates the note page's chat state. `noteChatFor(path)`
	// is a registry lookup, so the sheet and the panel are two views of one
	// transcript, and `beforeSend` comes down from the page unchanged — it
	// flushes the debounced save and throws on failure, which is the only thing
	// standing between OMNIA and a stale copy of the note.
	import BottomSheet from '$lib/BottomSheet.svelte';
	import ChatPanel from '$lib/ChatPanel.svelte';
	import { ensureNoteThread, noteChatFor, restoreNoteThread } from '$lib/stores.svelte';

	let {
		path,
		title,
		beforeSend,
		onclose
	}: {
		path: string;
		/** The note's name, for the header. */
		title: string;
		beforeSend?: () => Promise<void>;
		onclose: () => void;
	} = $props();

	// The panel tab has the same effect. Without it the drawer opens on an
	// empty transcript even when the note has a conversation behind it —
	// nothing loads the history until you send a message, so it reads as if
	// the thread had been deleted.
	$effect(() => {
		if (!path) return;
		void restoreNoteThread(path);
	});
</script>

<BottomSheet {title} label="Ask OMNIA about this note" {onclose}>
	<ChatPanel
		chat={noteChatFor(path)}
		ensureThread={() => ensureNoteThread(path)}
		contextPath={path}
		compact
		placeholder="Ask about this note…"
		starters={[
			'Summarize this note',
			'Suggest tags for it',
			'Make flashcards from it',
			'What should this link to?'
		]}
		{beforeSend}
	/>
</BottomSheet>
