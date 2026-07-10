import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import type { RuntimeConfig } from '$lib/runtime-config';

const { fetchLivePerceptionDetections } = vi.hoisted(() => ({
	fetchLivePerceptionDetections: vi.fn()
}));
vi.mock('$lib/api', () => ({ fetchLivePerceptionDetections }));

import LiveDetectionsPanel from '$lib/components/LiveDetectionsPanel.svelte';

function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<T>((resolvePromise, rejectPromise) => {
		resolve = resolvePromise;
		reject = rejectPromise;
	});
	return { promise, resolve, reject };
}

const config: RuntimeConfig = {
	apiBaseUrl: 'https://api.example.test',
	detectionsApiBaseUrl: 'https://api.example.test',
	detectionRoutes: {
		recent: '/detections/recent',
		byObject: '/detections/object/{object_id}',
		byGeohash: '/detections/geohash/{geohash}'
	},
	stateBaseUrl: 'https://api.example.test',
	statePath: '/state',
	mapDataPath: '/map-data',
	driveConfigPath: '/drive-config',
	demoVideosPath: '/demo-videos',
	videoCameraIds: ['ch1'],
	perceptionStreamUrls: {},
	perceptionStreamBaseUrl: 'https://perception.example.test',
	perceptionStreamPathTemplate: '/streams/{camera_id}.mjpg',
	cloudflareDriveWsUrl: 'wss://drive.example.test',
	tailscaleDriveWsUrl: 'wss://tail.example.test'
};

afterEach(() => {
	cleanup();
	fetchLivePerceptionDetections.mockReset();
});

describe('LiveDetectionsPanel producer freshness', () => {
	it('shows a current producer detection', async () => {
		fetchLivePerceptionDetections.mockResolvedValue({
			generated_at: new Date().toISOString(),
			cameras: {
				ch1: {
					updated_at: new Date().toISOString(),
					detections: [{ object_id: 'current-1', object_type: 'vehicle', confidence_score: 0.9 }]
				}
			}
		});
		render(LiveDetectionsPanel, { props: { config, refreshMs: 60_000 } });

		await waitFor(() => expect(screen.getByText('vehicle')).toBeInTheDocument());
		expect(screen.getByText(/1\/1 camera snapshots current/)).toBeInTheDocument();
		expect(screen.getByText(/CURRENT/)).toBeInTheDocument();
	});

	it('hides stale detections even when response generation time is current', async () => {
		fetchLivePerceptionDetections.mockResolvedValue({
			generated_at: new Date().toISOString(),
			cameras: {
				ch1: {
					updated_at: new Date(Date.now() - 60_000).toISOString(),
					detections: [{ object_id: 'stale-1', object_type: 'pedestrian', confidence_score: 0.9 }]
				}
			}
		});
		render(LiveDetectionsPanel, { props: { config, refreshMs: 60_000 } });

		await waitFor(() => expect(screen.getByText('Stale detection snapshot hidden')).toBeInTheDocument());
		expect(screen.queryByText('pedestrian')).not.toBeInTheDocument();
		expect(screen.getByText(/0\/1 camera snapshots current/)).toBeInTheDocument();
		expect(screen.getByText(/STALE/)).toBeInTheDocument();
	});

	it('does not let an older overlapping request overwrite the newest response', async () => {
		const first = deferred<Awaited<ReturnType<typeof fetchLivePerceptionDetections>>>();
		const second = deferred<Awaited<ReturnType<typeof fetchLivePerceptionDetections>>>();
		fetchLivePerceptionDetections
			.mockReturnValueOnce(first.promise)
			.mockReturnValueOnce(second.promise);
		render(LiveDetectionsPanel, { props: { config, refreshMs: 60_000 } });
		await waitFor(() => expect(fetchLivePerceptionDetections).toHaveBeenCalledTimes(1));

		screen.getByRole('button', { name: 'Refresh' }).click();
		await waitFor(() => expect(fetchLivePerceptionDetections).toHaveBeenCalledTimes(2));
		second.resolve({
			generated_at: new Date().toISOString(),
			cameras: {
				ch1: {
					updated_at: new Date().toISOString(),
					detections: [{ object_id: 'newest', object_type: 'vehicle', confidence_score: 0.9 }]
				}
			}
		});
		await waitFor(() => expect(screen.getByText('vehicle')).toBeInTheDocument());

		first.resolve({
			generated_at: new Date().toISOString(),
			cameras: {
				ch1: {
					updated_at: new Date().toISOString(),
					detections: [{ object_id: 'older', object_type: 'pedestrian', confidence_score: 0.8 }]
				}
			}
		});
		await Promise.resolve();
		expect(screen.getByText('vehicle')).toBeInTheDocument();
		expect(screen.queryByText('pedestrian')).not.toBeInTheDocument();
	});

	it('invalidates an in-flight request when its source disconnects', async () => {
		const disconnectedRequest = deferred<Awaited<ReturnType<typeof fetchLivePerceptionDetections>>>();
		const reconnectedRequest = deferred<Awaited<ReturnType<typeof fetchLivePerceptionDetections>>>();
		fetchLivePerceptionDetections
			.mockReturnValueOnce(disconnectedRequest.promise)
			.mockReturnValueOnce(reconnectedRequest.promise);
		const view = render(LiveDetectionsPanel, { props: { config, refreshMs: 60_000 } });
		await waitFor(() => expect(fetchLivePerceptionDetections).toHaveBeenCalledTimes(1));

		await view.rerender({ config: null, refreshMs: 60_000 });
		disconnectedRequest.resolve({
			generated_at: new Date().toISOString(),
			cameras: {
				ch1: {
					updated_at: new Date().toISOString(),
					detections: [{ object_id: 'ghost', object_type: 'pedestrian', confidence_score: 0.8 }]
				}
			}
		});
		await Promise.resolve();

		await view.rerender({ config, refreshMs: 60_000 });
		await waitFor(() => expect(fetchLivePerceptionDetections).toHaveBeenCalledTimes(2));
		expect(screen.queryByText('pedestrian')).not.toBeInTheDocument();
		reconnectedRequest.resolve({ generated_at: new Date().toISOString(), cameras: {} });
		await waitFor(() => expect(screen.queryByText('Loading live detector output...')).not.toBeInTheDocument());
	});
});
