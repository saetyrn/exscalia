// The note page decides "save this" or "the server's own text, already
// accounted for" from this annotation alone, so both halves of the round trip
// are pinned: a replacement carries it, typing never does.
import { describe, expect, it } from 'vitest';
import { isProgrammatic, programmaticChange } from './programmatic';
import { stateFrom } from './testing/cm';

describe('programmaticChange', () => {
	it('rides on a programmatic replacement', () => {
		const state = stateFrom('a|', []);
		const tr = state.update({
			changes: { from: 1, insert: 'b' },
			annotations: programmaticChange.of(true)
		});
		expect(tr.annotation(programmaticChange)).toBe(true);
		expect(isProgrammatic([tr])).toBe(true);
	});

	it('is absent from typing', () => {
		const state = stateFrom('a|', []);
		const tr = state.update(state.replaceSelection('b'), { userEvent: 'input.type' });
		expect(tr.annotation(programmaticChange)).toBeUndefined();
		expect(isProgrammatic([tr])).toBe(false);
	});

	it('counts an update as an edit when any doc change in it is one', () => {
		const state = stateFrom('a|', []);
		const replaced = state.update({
			changes: { from: 1, insert: 'b' },
			annotations: programmaticChange.of(true)
		});
		const typed = replaced.state.update(replaced.state.replaceSelection('c'), {
			userEvent: 'input.type'
		});
		expect(isProgrammatic([replaced, typed])).toBe(false);
	});

	it('ignores transactions that changed no text', () => {
		const state = stateFrom('a|', []);
		const moved = state.update({ selection: { anchor: 0 } });
		const replaced = moved.state.update({
			changes: { from: 1, insert: 'b' },
			annotations: programmaticChange.of(true)
		});
		expect(isProgrammatic([moved, replaced])).toBe(true);
		expect(isProgrammatic([moved])).toBe(false);
	});
});
