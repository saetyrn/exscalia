<script lang="ts">
	import { page } from '$app/state';
	import { api, encodePath } from '$lib/api';

	let tag = $derived(page.params.tag ?? '');
	let notes: { path: string; title: string; modified: string }[] = $state([]);

	$effect(() => {
		tag;
		api.notesForTag(tag).then((n) => (notes = n));
	});
</script>

<div class="tag-page">
	<h1>#{tag}</h1>
	{#if notes.length === 0}
		<p class="muted">No notes with this tag.</p>
	{:else}
		<ul>
			{#each notes as n (n.path)}
				<li>
					<a href="/note/{encodePath(n.path)}">{n.title || n.path}</a>
					<span class="mod">{n.modified?.slice(0, 10)}</span>
				</li>
			{/each}
		</ul>
	{/if}
</div>

<style>
	.tag-page {
		max-width: 720px;
		width: 100%;
		margin: 0 auto;
		padding: 28px 32px;
		overflow-y: auto;
	}
	h1 {
		font-size: 20px;
		color: var(--accent);
	}
	ul {
		list-style: none;
		padding: 0;
	}
	li {
		padding: 8px 0;
		border-bottom: 1px solid var(--border);
	}
	a {
		color: var(--text);
		font-weight: 600;
		text-decoration: none;
	}
	a:hover {
		color: var(--accent);
	}
	.mod {
		color: var(--text-muted);
		font-size: 12px;
		margin-left: 8px;
	}
	.muted {
		color: var(--text-muted);
	}

	@media (max-width: 760px) {
		.tag-page {
			padding: 18px 16px 24px;
		}
		h1 {
			margin-top: 0;
		}
		li {
			padding: 12px 2px;
		}
		.mod {
			display: block;
			margin: 2px 0 0;
		}
	}
</style>
