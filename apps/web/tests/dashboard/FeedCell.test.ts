import { act, cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import FeedCell from '$lib/components/FeedCell.svelte';
import type { TrackedObject } from '$lib/types';

class ControlledImage {
	static instances: ControlledImage[] = [];
	onload: (() => void) | null = null;
	onerror: (() => void) | null = null;
	src = '';

	constructor() {
		ControlledImage.instances.push(this);
	}
}

function tracked(overrides: Partial<TrackedObject> = {}): TrackedObject {
	return {
		object_id: 'object-1',
		object_type: 'vehicle',
		lat: 37.9,
		lon: -122.3,
		confidence: 0.9,
		street_name: 'test street',
		timestamp_utc: '2026-07-10T01:00:00.000Z',
		snapshot_url: 'https://example.test/snapshot-1.jpg',
		snapshot_timestamp: '2026-07-10T01:00:00.000Z',
		last_updated: Date.parse('2026-07-10T01:00:00.000Z'),
		...overrides
	};
}

beforeEach(() => {
	ControlledImage.instances = [];
	vi.stubGlobal('Image', ControlledImage);
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

describe('FeedCell snapshot ordering', () => {
	it('clears a loaded image when the producer removes its snapshot URL', async () => {
		const view = render(FeedCell, { props: { object: tracked() } });
		await waitFor(() => expect(ControlledImage.instances).toHaveLength(1));
		expect(ControlledImage.instances[0].src).toBe('https://example.test/snapshot-1.jpg');
		await act(() => ControlledImage.instances[0].onload?.());
		expect(screen.getByAltText('Snapshot of object-1')).toHaveAttribute(
			'src',
			'https://example.test/snapshot-1.jpg'
		);

		await view.rerender({
			object: tracked({ snapshot_url: null, snapshot_timestamp: null })
		});
		expect(screen.queryByAltText('Snapshot of object-1')).not.toBeInTheDocument();
		expect(screen.getByText('No snapshot')).toBeInTheDocument();
	});

	it('invalidates an in-flight preload when the snapshot is cleared', async () => {
		const view = render(FeedCell, { props: { object: tracked() } });
		await waitFor(() => expect(ControlledImage.instances).toHaveLength(1));

		await view.rerender({
			object: tracked({ snapshot_url: null, snapshot_timestamp: null })
		});
		await act(() => ControlledImage.instances[0].onload?.());

		expect(screen.queryByAltText('Snapshot of object-1')).not.toBeInTheDocument();
		expect(screen.getByText('No snapshot')).toBeInTheDocument();
	});

	it('ignores superseded preload callbacks and later out-of-order snapshots', async () => {
		const view = render(FeedCell, { props: { object: tracked() } });
		await waitFor(() => expect(ControlledImage.instances).toHaveLength(1));

		const newer = tracked({
			snapshot_url: 'https://example.test/snapshot-2.jpg',
			snapshot_timestamp: '2026-07-10T01:01:00.000Z'
		});
		await view.rerender({ object: newer });
		await waitFor(() => expect(ControlledImage.instances).toHaveLength(2));

		await act(() => ControlledImage.instances[0].onload?.());
		expect(screen.queryByAltText('Snapshot of object-1')).not.toBeInTheDocument();

		await act(() => ControlledImage.instances[1].onload?.());
		expect(screen.getByAltText('Snapshot of object-1')).toHaveAttribute(
			'src',
			'https://example.test/snapshot-2.jpg'
		);

		await view.rerender({ object: tracked() });
		expect(ControlledImage.instances).toHaveLength(2);
		expect(screen.getByAltText('Snapshot of object-1')).toHaveAttribute(
			'src',
			'https://example.test/snapshot-2.jpg'
		);
	});
});
