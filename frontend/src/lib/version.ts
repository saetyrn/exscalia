// Client/server version handshake. /api/auth/status names the server's API
// version and the oldest desktop build it still supports; the desktop compares
// that with its own version at launch.

/** What the server said about itself on the last status call. */
export const serverInfo: { apiVersion: number | null } = { apiVersion: null };

function parts(version: string): number[] {
	const core = version.trim().replace(/^v/i, '').split(/[-+]/, 1)[0];
	return core
		.split('.')
		.slice(0, 3)
		.map((part) => Number.parseInt(part, 10) || 0);
}

/** True when version `a` is older than `b`. Compares up to three dotted
 * numbers; a leading 'v' and any '-suffix' or '+build' are ignored. */
export function semverLt(a: string, b: string): boolean {
	const x = parts(a);
	const y = parts(b);
	for (let i = 0; i < 3; i++) {
		const d = (x[i] ?? 0) - (y[i] ?? 0);
		if (d) return d < 0;
	}
	return false;
}
