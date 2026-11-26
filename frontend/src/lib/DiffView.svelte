<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import { MergeView } from '@codemirror/merge';
	import { EditorState } from '@codemirror/state';
	import { EditorView, lineNumbers } from '@codemirror/view';

	let { oldContent, newContent }: { oldContent: string; newContent: string } = $props();

	let host: HTMLDivElement | undefined = $state();
	let view: MergeView | null = null;

	const shared = [
		EditorView.editable.of(false),
		EditorState.readOnly.of(true),
		EditorView.lineWrapping,
		lineNumbers(),
		EditorView.theme({
			'&': { fontSize: '12.5px', backgroundColor: 'transparent' },
			'.cm-content': { fontFamily: 'var(--font-mono)', color: 'var(--text)' },
			'.cm-gutters': {
				backgroundColor: 'var(--bg-panel)',
				color: 'var(--text-muted)',
				border: 'none'
			}
		})
	];

	$effect(() => {
		oldContent;
		newContent;
		if (!host) return;
		view?.destroy();
		view = new MergeView({
			a: { doc: untrack(() => oldContent), extensions: shared },
			b: { doc: untrack(() => newContent), extensions: shared },
			parent: host,
			gutter: true,
			highlightChanges: true,
			collapseUnchanged: { margin: 3, minSize: 4 }
		});
	});

	onDestroy(() => view?.destroy());
</script>

<div class="diff" bind:this={host}></div>

<style>
	.diff {
		border: 1px solid var(--border);
		border-radius: var(--radius);
		overflow: hidden;
		max-height: 480px;
		overflow-y: auto;
	}
	.diff :global(.cm-mergeView) {
		background: var(--bg);
	}
	.diff :global(.cm-changedLine),
	.diff :global(.cm-changedText) {
		background: color-mix(in srgb, var(--accent) 14%, transparent);
	}
	.diff :global(.cm-merge-a .cm-changedLine),
	.diff :global(.cm-merge-a .cm-changedText) {
		background: color-mix(in srgb, var(--removed) 14%, transparent);
	}
	.diff :global(.cm-merge-b .cm-changedLine),
	.diff :global(.cm-merge-b .cm-changedText) {
		background: color-mix(in srgb, var(--added) 14%, transparent);
	}

	@media (max-width: 760px) {
		.diff {
			max-height: 60dvh;
		}
		.diff :global(.cm-mergeViewEditors) {
			flex-direction: column;
		}
		.diff :global(.cm-mergeViewEditor) {
			flex-basis: auto;
			min-height: 160px;
		}
		.diff :global(.cm-merge-revert) {
			display: none;
		}
	}
</style>
