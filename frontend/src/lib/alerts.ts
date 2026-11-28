// Alerts stay queued on the server until the client acks them, so a poll can
// return one it already showed: a second poll that raced the first, or an ack
// that failed. This session-only set keeps each to a single toast.
const shown = new Set<string>();

/** The alerts not yet shown this session, recorded as shown. */
export function unshownAlerts<T extends { key: string }>(alerts: T[]): T[] {
	const fresh: T[] = [];
	for (const alert of alerts) {
		if (shown.has(alert.key)) continue;
		shown.add(alert.key);
		fresh.push(alert);
	}
	return fresh;
}
