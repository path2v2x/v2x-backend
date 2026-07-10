import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { fetchDetectionsPage, fetchDetectionsRange } = vi.hoisted(() => ({
	fetchDetectionsPage: vi.fn(),
	fetchDetectionsRange: vi.fn()
}));
vi.mock('$lib/api', () => ({ fetchDetectionsPage, fetchDetectionsRange }));

import RecentDetectionsPanel from '$lib/components/RecentDetectionsPanel.svelte';
import type { DetectionPage } from '$lib/types';

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((resolvePromise) => {
		resolve = resolvePromise;
	});
	return { promise, resolve };
}

afterEach(() => {
	cleanup();
	fetchDetectionsPage.mockReset();
	fetchDetectionsRange.mockReset();
});

describe('RecentDetectionsPanel request ordering', () => {
	it('labels strict schema-v2 media records separately from legacy rows', async () => {
		const mediaTimestamp = '2026-07-10T05:30:00.000Z';
		const trustedClock = {
			source: 'hls_ext_x_program_date_time',
			schema_version: 1,
			anchor_program_date_time_utc: '2026-07-10T05:29:59.000Z',
			position_milliseconds: 1000
		};
		fetchDetectionsPage.mockResolvedValue({
			items: [
				{
					object_id: 'trusted-car',
					object_type: 'car',
					timestamp_utc: mediaTimestamp,
					media_timestamp_utc: mediaTimestamp,
					media_time_trusted: true,
					timestamp_schema_version: 2,
					media_clock: trustedClock,
					decode_latency_ms: 5270.9
				},
				{
					object_id: 'timestamp-mismatch',
					object_type: 'car',
					timestamp_utc: '2026-07-10T06:30:00.000Z',
					media_timestamp_utc: mediaTimestamp,
					media_time_trusted: true,
					timestamp_schema_version: 2,
					media_clock: trustedClock
				},
				{
					object_id: 'boolean-schema-spoof',
					object_type: 'car',
					timestamp_utc: mediaTimestamp,
					media_timestamp_utc: mediaTimestamp,
					media_time_trusted: true,
					timestamp_schema_version: 2,
					media_clock: { ...trustedClock, schema_version: true as unknown as number }
				},
				{
					object_id: 'missing-provenance-spoof',
					object_type: 'car',
					timestamp_utc: mediaTimestamp,
					media_timestamp_utc: mediaTimestamp,
					media_time_trusted: true,
					timestamp_schema_version: 2,
					media_clock: {
						source: 'hls_ext_x_program_date_time',
						schema_version: 1
					}
				},
				{
					object_id: 'legacy-car',
					object_type: 'car',
					timestamp_utc: new Date().toISOString()
				}
			]
		});
		render(RecentDetectionsPanel, { props: { refreshMs: 60_000 } });
		await waitFor(() => expect(screen.getByText('trusted-car')).toBeInTheDocument());
		expect(screen.getAllByText('Trusted HLS')).toHaveLength(1);
		expect(screen.getByText('5271 ms')).toBeInTheDocument();
		expect(screen.getAllByText('Legacy / untrusted')).toHaveLength(4);
	});

	it('does not let an older overlapping live poll overwrite a refresh', async () => {
		const first = deferred<DetectionPage>();
		const second = deferred<DetectionPage>();
		fetchDetectionsPage.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
		render(RecentDetectionsPanel, { props: { refreshMs: 60_000 } });
		await waitFor(() => expect(fetchDetectionsPage).toHaveBeenCalledTimes(1));

		screen.getByRole('button', { name: 'Refresh' }).click();
		await waitFor(() => expect(fetchDetectionsPage).toHaveBeenCalledTimes(2));
		second.resolve({
			items: [{ object_id: 'newest', object_type: 'vehicle', timestamp_utc: new Date().toISOString() }]
		});
		await waitFor(() => expect(screen.getByText('newest')).toBeInTheDocument());

		first.resolve({
			items: [{ object_id: 'older', object_type: 'walker', timestamp_utc: new Date().toISOString() }]
		});
		await Promise.resolve();
		expect(screen.getByText('newest')).toBeInTheDocument();
		expect(screen.queryByText('older')).not.toBeInTheDocument();
	});

	it('invalidates the previous request when the selected range changes', async () => {
		const first = deferred<DetectionPage>();
		const second = deferred<DetectionPage>();
		fetchDetectionsRange.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
		const view = render(RecentDetectionsPanel, {
			props: {
				refreshMs: 60_000,
				range: { start: '2026-07-10T00:00:00Z', end: '2026-07-10T00:10:00Z' }
			}
		});
		await waitFor(() => expect(fetchDetectionsRange).toHaveBeenCalledTimes(1));

		await view.rerender({
			refreshMs: 60_000,
			range: { start: '2026-07-10T01:00:00Z', end: '2026-07-10T01:10:00Z' }
		});
		await waitFor(() => expect(fetchDetectionsRange).toHaveBeenCalledTimes(2));
		second.resolve({
			items: [{ object_id: 'range-b', object_type: 'vehicle', timestamp_utc: '2026-07-10T01:05:00Z' }]
		});
		await waitFor(() => expect(screen.getByText('range-b')).toBeInTheDocument());

		first.resolve({
			items: [{ object_id: 'range-a', object_type: 'walker', timestamp_utc: '2026-07-10T00:05:00Z' }]
		});
		await Promise.resolve();
		expect(screen.getByText('range-b')).toBeInTheDocument();
		expect(screen.queryByText('range-a')).not.toBeInTheDocument();
	});
});
