// The app's keyboard map, as data. Listeners still live where their features
// live, but what exists is declared HERE — the "?" cheatsheet renders this
// list, so a shortcut that isn't in it effectively doesn't exist.
import { isDesktop } from './runtime';

const mac =
	typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);
const MOD = mac ? '⌘' : 'Ctrl';
const SHIFT = mac ? '⇧' : 'Shift';
const ALT = mac ? '⌥' : 'Alt';

export interface ShortcutGroup {
	title: string;
	items: [keys: string, does: string][];
}

export function shortcutGroups(): ShortcutGroup[] {
	const groups: ShortcutGroup[] = [
		{
			title: 'Everywhere',
			items: [
				[`${MOD} K`, 'Command palette'],
				[`${MOD} M`, 'New note'],
				['?', 'Keyboard shortcuts'],
				['Esc', 'Close menus and dialogs']
			]
		},
		{
			title: 'Editor',
			items: [
				[`${MOD} B`, 'Bold'],
				[`${MOD} I`, 'Italic'],
				[`${MOD} U`, 'Underline'],
				[`${SHIFT} ${MOD} X`, 'Strikethrough'],
				[`${MOD} ${ALT} 1–6`, 'Heading level (again to remove)'],
				[`${SHIFT} ${MOD} K`, 'Insert link'],
				[`* _ ~ " '`, 'Wrap the selection'],
				['( [ `', 'Wrap the selection, or open a pair'],
				[`${MOD} F`, 'Find in note'],
				[`${MOD} S`, 'Save now'],
				[`${MOD} Z / ${SHIFT} ${MOD} Z`, 'Undo / redo'],
				['[[', 'Link to a note'],
				['/tasks text ⏎', 'Line becomes a task']
			]
		},
		{
			title: 'Lists & tasks',
			items: [
				['Tab', 'Nest the list item (or accept [[)'],
				[`${SHIFT} Tab`, 'Lift the list item out'],
				['⏎', 'Continue the list; again to leave it'],
				[`${MOD} ⏎`, 'Cycle the checkbox: none, [ ], [x]']
			]
		},
		{
			title: 'As you type',
			items: [
				['[] + space', 'Becomes a checkbox'],
				['-- + space', 'Becomes an em dash —'],
				['...', 'Becomes an ellipsis …']
			]
		},
		{
			title: 'Task board & lists',
			items: [
				[`${MOD} click`, 'Open in background tab'],
				['↑ ↓ ⏎', 'Move through search results']
			]
		},
		{
			title: 'Study',
			items: [
				['Space / ⏎', 'Reveal card'],
				['1–4', 'Grade card'],
				['Esc', 'Leave the deck']
			]
		}
	];
	if (isDesktop) {
		groups.splice(1, 0, {
			title: 'Tabs (desktop)',
			items: [
				[`${MOD} T`, 'New tab'],
				[`${MOD} W`, 'Close tab'],
				[`${MOD} 1–9`, 'Jump to tab'],
				['Ctrl Tab', 'Next tab']
			]
		});
	}
	return groups;
}
