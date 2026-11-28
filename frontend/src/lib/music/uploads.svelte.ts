// Music uploads: files and whole folders from this computer, sent in 8 MB
// pieces (backend/app/music_upload.py). Two files at a time; a piece that
// fails is retried with a growing wait, resuming from what the server says it
// has. Progress is in bytes. The queue lives at module level, so it survives
// navigating away from the Music page.
import { musicApi } from './api';

const AT_ONCE = 2;
const RETRIES = 5;
export const AUDIO_EXTENSIONS = ['.mp3', '.m4a', '.aac', '.flac', '.wav', '.ogg', '.opus', '.aif', '.aiff'];

export interface QueuedFile {
	file: File;
	/** The folder it goes into (null = the top level, so Unsorted). */
	folder: string | null;
	/** Folder names under `folder`, from a dropped folder's structure. */
	parts: string[];
}

interface Failure {
	name: string;
	error: string;
}

class Uploads {
	total = $state(0);
	sent = $state(0);
	files = $state(0);
	done = $state(0);
	failed = $state<Failure[]>([]);
	duplicates = $state<{ name: string; in: string[] }[]>([]);
	active = $derived(this.done < this.files);
	#queue: QueuedFile[] = [];
	#running = 0;

	add(items: QueuedFile[]) {
		const songs = items.filter((item) => isAudio(item.file.name));
		if (!songs.length) return 0;
		if (!this.active) {
			this.total = this.sent = this.files = this.done = 0;
			this.failed = [];
			this.duplicates = [];
		}
		for (const item of songs) {
			this.#queue.push(item);
			this.total += item.file.size;
			this.files += 1;
		}
		this.#pump();
		return songs.length;
	}

	#pump() {
		while (this.#running < AT_ONCE && this.#queue.length) {
			const item = this.#queue.shift()!;
			this.#running += 1;
			void this.#send(item).finally(() => {
				this.#running -= 1;
				this.done += 1;
				this.#pump();
			});
		}
	}

	async #send(item: QueuedFile) {
		const { file } = item;
		let counted = 0;
		const count = (received: number) => {
			this.sent += received - counted;
			counted = received;
		};
		try {
			const opened = await musicApi.openUpload({
				name: file.name,
				size: file.size,
				folder: item.folder,
				parts: item.parts
			});
			let offset = opened.received;
			let tries = 0;
			while (offset < file.size) {
				try {
					offset = await musicApi.uploadPiece(opened.id, offset, file.slice(offset, offset + opened.piece));
					count(offset);
					tries = 0;
				} catch (err) {
					if (++tries > RETRIES) throw err;
					await new Promise((r) => setTimeout(r, 1000 * 2 ** tries));
					// The answer may have been lost after the server took the piece.
					offset = (await musicApi.uploadStatus(opened.id)).received;
					count(offset);
				}
			}
			const report = await musicApi.finishUpload(opened.id);
			if (report.duplicates.length) this.duplicates = [...this.duplicates, ...report.duplicates];
			for (const fail of report.failed) this.failed = [...this.failed, fail];
		} catch (err) {
			this.sent += file.size - counted;
			this.failed = [...this.failed, { name: file.name, error: err instanceof Error ? err.message : String(err) }];
		}
	}
}

export const uploads = new Uploads();

export function isAudio(name: string): boolean {
	const lower = name.toLowerCase();
	return AUDIO_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

/** Files from a drop, walking dropped folders so their structure is kept. */
export async function filesFromDrop(data: DataTransfer, folder: string | null): Promise<QueuedFile[]> {
	const entries: FileSystemEntry[] = [];
	for (const item of Array.from(data.items ?? [])) {
		const entry = item.kind === 'file' ? item.webkitGetAsEntry?.() : null;
		if (entry) entries.push(entry);
	}
	if (!entries.length) {
		return Array.from(data.files ?? []).map((file) => ({ file, folder, parts: [] }));
	}
	const out: QueuedFile[] = [];
	const walk = async (entry: FileSystemEntry, parts: string[]): Promise<void> => {
		if (entry.isFile) {
			const file = await new Promise<File>((resolve, reject) =>
				(entry as FileSystemFileEntry).file(resolve, reject)
			);
			out.push({ file, folder, parts });
		} else if (entry.isDirectory) {
			const reader = (entry as FileSystemDirectoryEntry).createReader();
			const children: FileSystemEntry[] = [];
			// readEntries hands out at most 100 at a time.
			for (;;) {
				const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
					reader.readEntries(resolve, reject)
				);
				if (!batch.length) break;
				children.push(...batch);
			}
			for (const child of children) await walk(child, [...parts, entry.name]);
		}
	};
	for (const entry of entries) await walk(entry, []);
	return out;
}

/** Files from an <input webkitdirectory> or a plain multi-file input. */
export function filesFromInput(list: FileList, folder: string | null): QueuedFile[] {
	return Array.from(list).map((file) => {
		const rel = (file as File & { webkitRelativePath?: string }).webkitRelativePath ?? '';
		const parts = rel ? rel.split('/').slice(0, -1) : [];
		return { file, folder, parts };
	});
}

export function formatBytes(bytes: number): string {
	if (bytes < 1024 * 1024) return `${Math.max(0, Math.round(bytes / 1024))} KB`;
	if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
	return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
