// Server-sent-events over fetch. EventSource cannot send an Authorization
// header, so the desktop app reads SSE bodies through this parser instead;
// the OMNIA chat stream uses it everywhere.

export interface SseFrame {
	event: string;
	data: string;
}

/** Read an SSE response body to completion, invoking onFrame per event.
 * Comment-only frames (keepalives) are skipped, but every chunk read still
 * calls onActivity, so a caller can tell a quiet stream from a dead one. */
export async function readSseStream(
	response: Response,
	onFrame: (frame: SseFrame) => void,
	onActivity?: () => void
): Promise<void> {
	if (!response.body) throw new Error('Streaming is unavailable in this browser.');
	const reader = response.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	while (true) {
		const { done, value } = await reader.read();
		onActivity?.();
		buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
		// A proxy may re-frame lines as CRLF (the spec also allows a bare CR).
		// A CR at the very end may be half of a CRLF split across chunks, so it
		// waits for the next one.
		buffer = buffer.replace(/\r\n|\r(?!$)/g, '\n');
		const frames = buffer.split('\n\n');
		buffer = frames.pop() ?? '';
		for (const raw of frames) {
			let event = 'message';
			let data: string | null = null;
			for (const line of raw.split('\n')) {
				if (line.startsWith('event:')) event = line.slice(6).trim();
				if (line.startsWith('data:')) data = line.slice(5).trim();
			}
			if (data !== null || event !== 'message') onFrame({ event, data: data ?? '{}' });
		}
		if (done) break;
	}
}
