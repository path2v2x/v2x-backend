import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	isDriveConfigOverlayFresh,
	loadRuntimeConfig,
	resetRuntimeConfigCache
} from '$lib/runtime-config';

const NOW = Date.parse('2026-07-10T00:00:00Z');

function jsonResponse(body: unknown, ok = true): Response {
	return { ok, json: async () => body } as Response;
}

const staticConfig = {
	apiBaseUrl: 'https://api.example.test',
	driveConfigPath: '/drive-config',
	cloudflareDriveWsUrl: 'wss://static-drive.example.test',
	tailscaleDriveWsUrl: 'wss://static-tail.example.test'
};

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(NOW);
	window.history.replaceState({}, '', '/');
	sessionStorage.clear();
	resetRuntimeConfigCache();
});

afterEach(() => {
	vi.unstubAllGlobals();
	vi.useRealTimers();
	resetRuntimeConfigCache();
});

describe('drive config overlay', () => {
	it('requires a bounded, unexpired producer validity window', () => {
		expect(isDriveConfigOverlayFresh({
			updatedAt: '2026-07-09T23:59:00Z',
			expiresAt: '2026-07-10T01:00:00Z'
		}, NOW)).toBe(true);
		expect(isDriveConfigOverlayFresh({ expiresAt: '2026-07-10T01:00:00Z' }, NOW)).toBe(false);
		expect(isDriveConfigOverlayFresh({
			updatedAt: '2026-07-09T20:00:00Z',
			expiresAt: '2026-07-09T23:00:00Z'
		}, NOW)).toBe(false);
		expect(isDriveConfigOverlayFresh({
			updatedAt: '2026-07-10T00:00:00Z',
			expiresAt: '2026-07-12T00:00:00Z'
		}, NOW)).toBe(false);
	});

	it('applies a fresh versioned overlay', async () => {
		vi.stubGlobal('fetch', vi.fn()
			.mockResolvedValueOnce(jsonResponse(staticConfig))
			.mockResolvedValueOnce(jsonResponse({
				version: 7,
				updatedAt: '2026-07-09T23:59:00Z',
				expiresAt: '2026-07-10T01:00:00Z',
				source: 'quick_tunnel',
				cloudflareDriveWsUrl: 'wss://current-drive.example.test'
			})));

		const config = await loadRuntimeConfig();
		expect(config.cloudflareDriveWsUrl).toBe('wss://current-drive.example.test');
		expect(config.tailscaleDriveWsUrl).toBe('wss://static-tail.example.test');
		expect(config.driveConfigVersion).toBe(7);
	});

	it('rejects expired, unversioned, or credential-bearing overlays', async () => {
		vi.stubGlobal('fetch', vi.fn()
			.mockResolvedValueOnce(jsonResponse(staticConfig))
			.mockResolvedValueOnce(jsonResponse({
				updatedAt: '2026-07-09T23:00:00Z',
				expiresAt: '2026-07-10T01:00:00Z',
				cloudflareDriveWsUrl: 'wss://user:secret@bad.example.test'
			})));

		const config = await loadRuntimeConfig();
		expect(config.cloudflareDriveWsUrl).toBe('wss://static-drive.example.test');
		expect(config.tailscaleDriveWsUrl).toBe('wss://static-tail.example.test');
		expect(config.driveConfigVersion).toBeUndefined();
	});

	it('rejects an out-of-order version during the browser session', async () => {
		const fetchMock = vi.fn()
			.mockResolvedValueOnce(jsonResponse(staticConfig))
			.mockResolvedValueOnce(jsonResponse({
				version: 7,
				updatedAt: '2026-07-09T23:59:00Z',
				expiresAt: '2026-07-10T01:00:00Z',
				cloudflareDriveWsUrl: 'wss://version-7.example.test'
			}))
			.mockResolvedValueOnce(jsonResponse(staticConfig))
			.mockResolvedValueOnce(jsonResponse({
				version: 6,
				updatedAt: '2026-07-09T23:59:30Z',
				expiresAt: '2026-07-10T01:00:00Z',
				cloudflareDriveWsUrl: 'wss://version-6.example.test'
			}));
		vi.stubGlobal('fetch', fetchMock);

		expect((await loadRuntimeConfig()).driveConfigVersion).toBe(7);
		resetRuntimeConfigCache();
		const second = await loadRuntimeConfig();
		expect(second.driveConfigVersion).toBeUndefined();
		expect(second.cloudflareDriveWsUrl).toBe('wss://static-drive.example.test');
	});
});
