// Marks a document change the app made on the user's behalf — the server's
// normalized text coming back after a save, a remote edit adopted or merged, a
// rename's realigned frontmatter — as opposed to one the user made.
//
// The note page used to tell the two apart with a one-shot flag set just
// before `replaceDoc`, consumed by whatever change arrived next. A replacement
// that produced no transaction (identical text, no view yet) or that was held
// for an IME composition left the flag armed, and it swallowed the user's next
// real edit: never drafted, never saved. The annotation travels on the
// transaction itself, so there is nothing left armed to go wrong.
import { Annotation, type Transaction } from '@codemirror/state';

export const programmaticChange = Annotation.define<boolean>();

/** True when every transaction that changed the document was a programmatic
 *  replacement. A user edit batched alongside one still makes the update a
 *  user edit: it has to be saved. */
export function isProgrammatic(transactions: readonly Transaction[]): boolean {
	const changes = transactions.filter((tr) => tr.docChanged);
	return (
		changes.length > 0 && changes.every((tr) => tr.annotation(programmaticChange) === true)
	);
}
