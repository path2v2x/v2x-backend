import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
	clearCoverageChunkCache,
	fetchCameraCoverageSequentially
} from '$lib/video-coverage';
import type { VideoCoverage } from '$lib/types';

const HOUR = 60 * 60 * 1000;
const START = Date.parse('2026-07-10T00:00:00Z');

describe('camera coverage batching', () => {
	beforeEach(() => {
		clearCoverageChunkCache();
		vi.useRealTimers();
	});

	it('never overlaps ListFragments requests for one camera', async () => {
		let active = 0;
		let maximumActive = 0;
		const fetcher = vi.fn(async (cameraId: string, window: { start: string; end: string }) => {
			active += 1;
			maximumActive = Math.max(maximumActive, active);
			await Promise.resolve();
			active -= 1;
			return {
				cameraId,
				start: window.start,
				end: window.end,
				intervals: [{ start: window.start, end: window.end }],
				fragmentCount: 1,
				truncated: false
			} satisfies VideoCoverage;
		});

		const result = await fetchCameraCoverageSequentially(
			'ch2',
			START,
			START + 12 * HOUR,
			4 * HOUR,
			fetcher
		);

		expect(fetcher).toHaveBeenCalledTimes(3);
		expect(maximumActive).toBe(1);
		expect(result.fragmentCount).toBe(3);
		expect(result.intervals).toHaveLength(1);
		expect(result.truncated).toBe(false);
	});

	it('retains successful chunks and marks a partial query truncated', async () => {
		let call = 0;
		const result = await fetchCameraCoverageSequentially(
			'ch1',
			START,
			START + 8 * HOUR,
			4 * HOUR,
			async (_cameraId, window) => {
				call += 1;
				if (call === 2) throw new Error('transient KVS limit');
				return {
					cameraId: 'ch1',
					start: window.start,
					end: window.end,
					intervals: [{ start: window.start, end: window.end }],
					fragmentCount: 4,
					truncated: false
				};
			}
		);

		expect(result.fragmentCount).toBe(4);
		expect(result.intervals).toHaveLength(1);
		expect(result.truncated).toBe(true);
	});

	it('aligns rolling windows and reuses only complete historical chunks', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(START + 10 * HOUR);
		const fetcher = vi.fn(async (cameraId: string, window: { start: string; end: string }) => ({
			cameraId,
			start: window.start,
			end: window.end,
			intervals: [{ start: window.start, end: window.end }],
			fragmentCount: 1,
			truncated: false
		}));

		const first = await fetchCameraCoverageSequentially(
			'ch4',
			START + HOUR,
			START + 9 * HOUR,
			4 * HOUR,
			fetcher
		);
		const second = await fetchCameraCoverageSequentially(
			'ch4',
			START + HOUR + 60_000,
			START + 9 * HOUR + 60_000,
			4 * HOUR,
			fetcher
		);

		expect(fetcher).toHaveBeenCalledTimes(4);
		expect(fetcher.mock.calls.slice(0, 3).map((call) => call[1])).toEqual([
			{ start: '2026-07-10T00:00:00.000Z', end: '2026-07-10T04:00:00.000Z' },
			{ start: '2026-07-10T04:00:00.000Z', end: '2026-07-10T08:00:00.000Z' },
			{ start: '2026-07-10T08:00:00.000Z', end: '2026-07-10T12:00:00.000Z' }
		]);
		expect(first.intervals[0].start).toBe('2026-07-10T01:00:00.000Z');
		expect(first.intervals[0].end).toBe('2026-07-10T09:00:00.000Z');
		expect(second.intervals[0].start).toBe('2026-07-10T01:01:00.000Z');
		expect(second.intervals[0].end).toBe('2026-07-10T09:01:00.000Z');
	});

	it('never caches a truncated historical chunk', async () => {
		vi.useFakeTimers();
		vi.setSystemTime(START + 24 * HOUR);
		const fetcher = vi.fn(async (cameraId: string, window: { start: string; end: string }) => ({
			cameraId,
			start: window.start,
			end: window.end,
			intervals: [],
			fragmentCount: 0,
			truncated: true
		}));

		await fetchCameraCoverageSequentially('ch1', START, START + 4 * HOUR, 4 * HOUR, fetcher);
		await fetchCameraCoverageSequentially('ch1', START, START + 4 * HOUR, 4 * HOUR, fetcher);

		expect(fetcher).toHaveBeenCalledTimes(2);
	});
});
