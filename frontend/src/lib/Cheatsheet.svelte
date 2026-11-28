<script lang="ts">
	// The "?" overlay. Renders lib/shortcuts.ts — the single declaration of
	// what the keyboard does — grouped, monochrome, dismissed by Esc, ?, or a
	// click anywhere outside.
	import { fade } from '$lib/motion';
	import { appShell, modal } from './modal';
	import { shortcutGroups } from './shortcuts';

	let open = $state(false);
	const groups = shortcutGroups();

	function typingTarget(target: EventTarget | null): boolean {
		if (!(target instanceof HTMLElement)) return false;
		if (target.closest('.cm-editor')) return true;
		const tag = target.tagName;
		return tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable;
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key !== '?' || event.metaKey || event.ctrlKey || event.altKey) return;
		if (typingTarget(event.target)) return;
		event.preventDefault();
		open = !open;
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#if open}
	<div
		class="scrim"
		role="presentation"
		transition:fade={{ duration: 120 }}
		onclick={() => (open = false)}
	>
		<!-- The click handler only fences the scrim's close-on-click; Esc
		     already covers the keyboard. The sheet is the modal node, so it
		     takes focus itself and its scroll area answers the arrow keys. -->
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div
			class="sheet"
			role="dialog"
			aria-modal="true"
			aria-label="Keyboard shortcuts"
			tabindex="-1"
			onclick={(e) => e.stopPropagation()}
			use:modal={{ onEscape: () => (open = false), inert: appShell() }}
		>
			<h2>Keyboard</h2>
			<div class="groups">
				{#each groups as group (group.title)}
					<div class="group">
						<h3>{group.title}</h3>
						<dl>
							{#each group.items as [keys, does] (keys + does)}
								<dt><kbd>{keys}</kbd></dt>
								<dd>{does}</dd>
							{/each}
						</dl>
					</div>
				{/each}
			</div>
		</div>
	</div>
{/if}

<style>
	.scrim {
		position: fixed;
		inset: 0;
		z-index: var(--z-dialog);
		background: color-mix(in srgb, #000 45%, transparent);
		display: grid;
		place-items: center;
		padding: 24px;
	}
	.sheet {
		background: var(--bg-panel);
		border: 1px solid var(--border);
		border-radius: 12px;
		padding: 20px 24px;
		max-width: 680px;
		width: 100%;
		max-height: 80vh;
		overflow-y: auto;
		box-shadow: var(--lift-shadow);
	}
	h2 {
		margin: 0 0 14px;
		font-size: 13px;
		letter-spacing: 0.5px;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.groups {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		gap: 18px 32px;
	}
	h3 {
		margin: 0 0 8px;
		font-size: 12px;
		font-weight: 600;
		color: var(--text-muted);
	}
	dl {
		margin: 0;
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 6px 12px;
		align-items: baseline;
	}
	dt {
		text-align: right;
	}
	dd {
		margin: 0;
		font-size: 13px;
		color: var(--text);
	}
	kbd {
		font: 11px var(--font-mono);
		color: var(--text-muted);
		background: var(--bg);
		border: 1px solid var(--border);
		border-radius: 5px;
		padding: 2px 6px;
		white-space: nowrap;
	}
</style>
