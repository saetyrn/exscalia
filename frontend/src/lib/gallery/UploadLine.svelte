<script lang="ts">
	// "Saving 3 of 12" while pictures go up; nothing otherwise.
	import { uploading, uploads } from './uploads.svelte';
</script>

{#if uploading()}
	<p class="line" role="status">Saving {uploads.done + uploads.failed + 1} of {uploads.total}</p>
{/if}

<style>
	.line {
		margin: 0 0 14px;
		font-size: 13px;
		color: var(--text-muted);
	}
</style>
