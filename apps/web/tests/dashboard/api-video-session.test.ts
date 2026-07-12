import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/runtime-config', () => ({
	buildAssetUrl: vi.fn(),
	loadRuntimeConfig: vi.fn(async () => ({
		apiBaseUrl: 'https://api.example.test/'
	}))
}));

import { fetchVideoSession } from '$lib/api';

beforeEach(() => {
	vi.stubGlobal(
		'fetch',
		vi.fn(async () => ({
			ok: true,
			json: async () => ({
				cameraId: 'ch 1',
				playbackMode: 'LIVE',
				hlsUrl: 'https://api.example.test/video/proxy/opaque/master',
				delivery: 'SAME_ORIGIN_PROXY',
				expiresIn: 300,
				region: 'us-west-2'
			})
		}))
	);
});

afterEach(() => {
	vi.unstubAllGlobals();
});

describe('browser video sessions', () => {
	it('uses the opaque browser-only route and preserves archive bounds', async () => {
		await fetchVideoSession('ch 1', {
			start: '2026-07-10T05:00:00Z',
			end: '2026-07-10T05:15:00Z'
		});

		expect(fetch).toHaveBeenCalledOnce();
		const [url, options] = vi.mocked(fetch).mock.calls[0];
		expect(String(url)).toBe(
			'https://api.example.test/video/browser-session/ch%201?start=2026-07-10T05%3A00%3A00Z&end=2026-07-10T05%3A15%3A00Z'
		);
		expect(options).toEqual({ cache: 'no-store' });
	});
});
