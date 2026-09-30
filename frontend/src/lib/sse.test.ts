// The SSE parser behind the desktop live stream and every OMNIA chat stream.
// Network chunks land wherever they like, so frames are fed in awkward pieces.
import { describe, expect, it } from 'vitest';
import { readSseStream, type SseFrame } from './sse';

function streamOf(chunks: (string | Uint8Array)[]): Response {
	const encoder = new TextEncoder();
	return new Response(
		new ReadableStream<Uint8Array>({
			start(controller) {
				for (const chunk of chunks) {
					controller.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk);
				}
				controller.close();
			}
		})
	);
}

async function framesOf(chunks: (string | Uint8Array)[]): Promise<SseFrame[]> {
	const frames: SseFrame[] = [];
	await readSseStream(streamOf(chunks), (frame) => frames.push(frame));
	return frames;
}

describe('readSseStream', () => {
	it('reassembles a frame split mid-line', async () => {
		expect(await framesOf(['event: no', 'te\ndata: {"pa', 'th":"a.md"}\n', '\n'])).toEqual([
			{ event: 'note', data: '{"path":"a.md"}' }
		]);
	});

	it('reassembles a character split between its UTF-8 bytes', async () => {
		const bytes = new TextEncoder().encode('data: {"path":"café.md"}\n\n');
		const cut = bytes.indexOf(0xc3) + 1; // between the two bytes of é
		expect(await framesOf([bytes.slice(0, cut), bytes.slice(cut)])).toEqual([
			{ event: 'message', data: '{"path":"café.md"}' }
		]);
	});

	it('skips comment-only keepalives', async () => {
		expect(await framesOf([': keepalive\n\n', ': keepalive\n\n'])).toEqual([]);
	});

	it('passes an event without data as an empty object', async () => {
		expect(await framesOf(['event: ready\n\n'])).toEqual([{ event: 'ready', data: '{}' }]);
	});

	it('reads two frames from one chunk', async () => {
		expect(await framesOf(['event: ready\ndata: {}\n\nevent: note\ndata: {"a":1}\n\n'])).toEqual([
			{ event: 'ready', data: '{}' },
			{ event: 'note', data: '{"a":1}' }
		]);
	});

	it('reads CRLF frames, including a CRLF split across chunks', async () => {
		expect(
			await framesOf([
				'event: ready\r\ndata: {}\r\n\r\n: keepalive\r\n\r\n',
				'event: note\r\ndata: {"a":1}\r',
				'\n\r',
				'\n'
			])
		).toEqual([
			{ event: 'ready', data: '{}' },
			{ event: 'note', data: '{"a":1}' }
		]);
	});

	it('drops an unterminated frame at the end of the stream', async () => {
		expect(await framesOf(['data: {"a":1}\n\ndata: {"b":2}'])).toEqual([
			{ event: 'message', data: '{"a":1}' }
		]);
	});
});
