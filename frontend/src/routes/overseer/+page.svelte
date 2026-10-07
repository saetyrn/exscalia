<script lang="ts">
	import { goto } from '$app/navigation';
	import { onMount } from 'svelte';

	onMount(() => {
		void goto('/omnia', { replaceState: true });
	});
</script>

<svelte:head>
	<title>OMNIA — EXSCALIA</title>
</svelte:head>

<p class="redirecting">Opening OMNIA…</p>

<style>
	.redirecting {
		margin: auto;
		color: var(--text-muted);
		font-size: 13px;
	}
</style>
