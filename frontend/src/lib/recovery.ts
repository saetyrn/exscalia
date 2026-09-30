// What to do with a draft this device left behind, given the server copy the
// note just opened with.
//
// The draft used to be put back verbatim whenever it differed from the server,
// and 'Keep and save' then went out as a fast-forward on top of whatever the
// server held — so a draft left on the phone replaced edits made on the
// desktop in the meantime. A draft carries the text it was edited from, which
// is the common ancestor of both sides: the same three-way merge the live
// stream uses folds the draft onto the newer version, and only edits to the
// same ground have to be settled by hand.
import type { LocalDraft } from './drafts';
import { alignManagedStamps, sameExceptManagedStamps } from './frontmatter';
import { mergeThreeWay } from './merge';

export type DraftRecovery =
	/** Nothing to recover: no draft, or the server already holds its text. */
	| { kind: 'none' }
	/** Open `doc` as unsaved work on top of the loaded version. */
	| { kind: 'recover'; doc: string }
	/** The draft and the server rewrote the same lines. `doc` is the draft as
	 * written and `base` what it was edited from, so the draft can be rewritten
	 * unchanged until someone picks a side. */
	| { kind: 'conflict'; doc: string; base: { content: string; revision: string } };

export function reconcileDraft(
	draft: LocalDraft | null,
	loaded: { content: string; revision: string }
): DraftRecovery {
	// The server stamps `modified` on every save, so a draft whose PUT did land
	// still differs from it by that one line.
	if (!draft || sameExceptManagedStamps(draft.content, loaded.content)) return { kind: 'none' };
	// Edited on the version just loaded; or a draft from before drafts carried
	// their base, which has nothing to merge from.
	if (draft.baseRevision === loaded.revision || draft.baseContent === undefined) {
		return { kind: 'recover', doc: draft.content };
	}
	// The editor's copy of the managed stamps can lag its base by a save; left
	// as is, that lag reads as an edit to the same line the server rewrote.
	const merged = mergeThreeWay(
		draft.baseContent,
		alignManagedStamps(draft.content, draft.baseContent),
		loaded.content
	);
	if (!merged.ok) {
		return {
			kind: 'conflict',
			doc: draft.content,
			base: { content: draft.baseContent, revision: draft.baseRevision }
		};
	}
	// Everything the draft added already made it to the server.
	if (sameExceptManagedStamps(merged.text, loaded.content)) return { kind: 'none' };
	return { kind: 'recover', doc: merged.text };
}
