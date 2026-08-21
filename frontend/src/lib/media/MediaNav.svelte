<script lang="ts">
	import { page } from '$app/state';

	// The Letterboxd-style section strip shared by every hub page. Each page
	// places it where its layout wants (the hub under the profile header,
	// sub-pages at the top).
	const LINKS = [
		{ href: '/media', label: 'Profile' },
		{ href: '/media/library', label: 'Library' },
		{ href: '/media/activity', label: 'Activity' },
		{ href: '/media/watchlist', label: 'Watchlist' }
	];
</script>

<nav class="media-nav" aria-label="Media sections">
	{#each LINKS as link (link.href)}
		<a href={link.href} class:active={page.url.pathname === link.href}>{link.label}</a>
	{/each}
</nav>

<style>
	.media-nav {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-bottom: 16px;
		border: 1px solid var(--border);
		border-radius: 10px;
		padding: 4px;
		width: fit-content;
	}
	.media-nav a {
		padding: 5px 14px;
		font-size: 13px;
		border-radius: 7px;
		text-decoration: none;
		color: var(--text-muted);
		transition: color var(--motion-fast), background-color var(--motion-fast);
	}
	.media-nav a:hover {
		color: var(--text);
	}
	.media-nav a.active {
		color: var(--text);
		background: var(--bg-panel);
	}
</style>
