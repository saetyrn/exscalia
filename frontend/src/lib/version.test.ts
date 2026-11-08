import { describe, expect, it } from 'vitest';
import { semverLt } from './version';

describe('semverLt', () => {
	it('orders patch, minor and major numerically', () => {
		expect(semverLt('0.1.29', '0.1.30')).toBe(true);
		expect(semverLt('0.1.9', '0.1.10')).toBe(true);
		expect(semverLt('0.2.0', '0.1.30')).toBe(false);
		expect(semverLt('1.0.0', '0.9.99')).toBe(false);
	});

	it('is false for equal versions', () => {
		expect(semverLt('0.1.29', '0.1.29')).toBe(false);
		expect(semverLt('0.1', '0.1.0')).toBe(false);
	});

	it('tolerates a leading v and a suffix', () => {
		expect(semverLt('v0.1.29', '0.1.30')).toBe(true);
		expect(semverLt('0.1.30-beta', '0.1.30')).toBe(false);
		expect(semverLt('0.1.29', '0.1.30-beta')).toBe(true);
		expect(semverLt('0.1.30+abc1234', 'v0.1.30')).toBe(false);
	});

	// The server's default minimum never flags a real build.
	it('treats 0.0.0 as no minimum', () => {
		expect(semverLt('0.1.29', '0.0.0')).toBe(false);
	});
});
