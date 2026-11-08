// The SSE-over-fetch parser. Keepalive comments carry no frame, but the
// desktop live connection's idle watchdog needs to see them arrive.
import { describe, expect, it } from 'vitest';
import { readSseStream, type SseFrame } from './sse';

/** A response whose body arrives in exactly these chunks. */
function streamOf(chunks: string[]): Response {
	const encoder = new TextEncoder();
	return new Response(
		new ReadableStream<Uint8Array>({
			start(controller) {
				for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
				controller.close();
			}
		})
	);
}

describe('readSseStream', () => {
	it('reports activity for every chunk, keepalive comments included, before parsing it', async () => {
		const log: string[] = [];
		await readSseStream(
			streamOf(['event: ready\ndata: {}\n\n', ': keepalive\n\n', 'event: note\ndata: {"path":"a.md"}\n\n']),
			({ event }) => log.push(event),
			() => log.push('activity')
		);

		// The last 'activity' is the read that finds the stream closed.
		expect(log).toEqual(['activity', 'ready', 'activity', 'activity', 'note', 'activity']);
	});

	it('still parses frames split across chunks for two-argument callers', async () => {
		const frames: SseFrame[] = [];
		await readSseStream(
			streamOf(['event: no', 'te\ndata: {"a":', '1}\n\n: keepalive\n\n', 'data: {"b":2}\n\n']),
			(frame) => frames.push(frame)
		);

		expect(frames).toEqual([
			{ event: 'note', data: '{"a":1}' },
			{ event: 'message', data: '{"b":2}' }
		]);
	});
});
