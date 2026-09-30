<script lang="ts">
	import { tick } from 'svelte';
	import { fade, fly } from '$lib/motion';
	import Cheatsheet from './Cheatsheet.svelte';
	import CommandPalette from './CommandPalette.svelte';
	import { appShell, modal } from './modal';
	import { closeDialog, dismissToast, pauseToast, resumeToast, ui } from './ui.svelte';

	let value = $state('');
	let input: HTMLInputElement | undefined = $state();
	let panel: HTMLDivElement | undefined = $state();

	$effect(() => {
		const id = ui.dialog?.id;
		if (!id) return;
		value = ui.dialog?.value ?? '';
	});

	function submit() {
		if (!ui.dialog) return;
		if (ui.dialog.kind === 'prompt') {
			const result = value.trim();
			if (!result) return;
			closeDialog(result);
		} else {
			closeDialog(true);
		}
	}
</script>

<CommandPalette />
<Cheatsheet />

{#if ui.dialog}
	<button
		class="dialog-scrim"
		type="button"
		aria-label="Cancel"
		onclick={() => closeDialog(ui.dialog?.kind === 'confirm' ? false : null)}
		transition:fade={{ duration: 120 }}
	></button>
	<div class="dialog-wrap" role="presentation">
		<!-- Keyed so a dialog that replaces another (or reopens during the
		     scrim's fade) is a fresh modal: its own initial focus and return.
		     The fly is |global because a local intro inside the key would
		     only play on a replacement, never on the first open. -->
		{#key ui.dialog.id}
		<div
			class="dialog"
			role="dialog"
			aria-modal="true"
			aria-labelledby="dialog-title"
			tabindex="-1"
			bind:this={panel}
			use:modal={{
				// A confirm focuses the panel, not a button, so a stray Enter
				// cannot answer it.
				initialFocus: () => (ui.dialog?.kind === 'prompt' ? input : panel),
				onEscape: () => closeDialog(ui.dialog?.kind === 'confirm' ? false : null),
				inert: appShell()
			}}
			in:fly|global={{ y: 8, duration: 170 }}
		>
			<h2 id="dialog-title">{ui.dialog.title}</h2>
			<p>{ui.dialog.message}</p>
			{#if ui.dialog.kind === 'prompt'}
				<input
					bind:this={input}
					bind:value
					placeholder={ui.dialog.placeholder}
					onkeydown={(event) => {
						if (event.key === 'Enter') {
							event.preventDefault();
							submit();
						}
					}}
				/>
			{/if}
			<div class="dialog-actions">
				<button onclick={() => closeDialog(ui.dialog?.kind === 'confirm' ? false : null)}>
					{ui.dialog.cancelLabel}
				</button>
				<button
					class:primary={!ui.dialog.danger}
					class:danger={ui.dialog.danger}
					disabled={ui.dialog.kind === 'prompt' && !value.trim()}
					onclick={submit}
				>
					{ui.dialog.confirmLabel}
				</button>
			</div>
		</div>
		{/key}
	</div>
{/if}

<div class="toast-stack" aria-live="polite" aria-atomic="false">
	{#each ui.toasts as toast (toast.id)}
		<!-- The dismiss timer holds while the pointer or focus is on the toast,
		     so its action stays reachable for as long as someone is using it. -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="toast"
			class:error={toast.tone === 'error'}
			class:success={toast.tone === 'success'}
			in:fly={{ x: 18, duration: 180 }}
			onpointerenter={() => pauseToast(toast.id)}
			onpointerleave={(event) => {
				if (!event.currentTarget.contains(document.activeElement)) resumeToast(toast.id);
			}}
			onfocusin={() => pauseToast(toast.id)}
			onfocusout={(event) => {
				const el = event.currentTarget;
				if (!el.contains(event.relatedTarget as Node | null) && !el.matches(':hover')) resumeToast(toast.id);
			}}
		>
			<span>{toast.message}</span>
			{#if toast.action && toast.actionLabel}
				<button
					onclick={() => {
						toast.action?.();
						dismissToast(toast.id);
					}}>{toast.actionLabel}</button
				>
			{/if}
			<button class="toast-close" aria-label="Dismiss" onclick={() => dismissToast(toast.id)}>×</button>
		</div>
	{/each}
</div>

<style>
	.dialog-scrim {
		position: fixed;
		inset: 0;
		z-index: var(--z-dialog);
		border: 0;
		border-radius: 0;
		background: color-mix(in srgb, #000 48%, transparent);
		backdrop-filter: blur(3px);
	}
	.dialog-wrap {
		position: fixed;
		inset: 0;
		z-index: calc(var(--z-dialog) + 1);
		display: grid;
		place-items: center;
		padding: 18px;
		pointer-events: none;
	}
	.dialog {
		width: min(440px, 100%);
		padding: 20px;
		border: 1px solid var(--border);
		border-radius: 14px;
		background: var(--bg-panel);
		color: var(--text);
		box-shadow: 0 22px 70px color-mix(in srgb, #000 35%, transparent);
		pointer-events: auto;
	}
	.dialog h2 {
		margin: 0 0 8px;
		font-size: 18px;
	}
	.dialog p {
		margin: 0 0 16px;
		color: var(--text-muted);
		line-height: 1.45;
	}
	.dialog input {
		width: 100%;
	}
	.dialog-actions {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		margin-top: 18px;
	}
	.toast-stack {
		position: fixed;
		z-index: var(--z-toast);
		right: 16px;
		bottom: 16px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		width: min(380px, calc(100vw - 32px));
		pointer-events: none;
	}
	.toast {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-left: 3px solid var(--accent);
		border-radius: 9px;
		background: var(--bg-panel);
		color: var(--text);
		box-shadow: 0 10px 30px color-mix(in srgb, #000 22%, transparent);
		font-size: 13px;
		pointer-events: auto;
	}
	.toast span {
		flex: 1;
	}
	.toast.error {
		border-left-color: var(--danger);
	}
	.toast.success {
		border-left-color: var(--added);
	}
	.toast button {
		padding: 3px 6px;
		color: var(--accent);
		font-weight: 700;
	}
	.toast .toast-close {
		color: var(--text-muted);
		font-size: 17px;
	}
	@media (max-width: 760px) {
		.dialog-wrap {
			align-items: end;
			padding: 0;
		}
		.dialog {
			width: 100%;
			border-radius: 16px 16px 0 0;
			padding: 20px var(--m-gutter) calc(20px + env(safe-area-inset-bottom));
		}
		/* The prompt field carries no type attribute, so app.css's mobile floor
		   (which selects on input[type='text']) never reached it and it stayed
		   at 14px — enough for iOS to zoom the page the moment it focused. */
		.dialog input {
			min-height: 44px;
			font-size: var(--m-row-text);
		}
		.toast-stack {
			/* On the page gutter, so a toast lines up with the content it is
			   reporting on rather than sitting 6px proud of it. */
			right: var(--m-gutter);
			/* Above the tab bar, which already covers the home indicator — the
			   safe-area inset is inside --tabbar-h, not added to it. */
			bottom: calc(10px + var(--tabbar-h) + var(--mediabar-h, 0px));
			width: calc(100vw - 2 * var(--m-gutter));
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.dialog-scrim {
			backdrop-filter: none;
		}
	}
</style>
