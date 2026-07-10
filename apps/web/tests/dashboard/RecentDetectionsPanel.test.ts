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
