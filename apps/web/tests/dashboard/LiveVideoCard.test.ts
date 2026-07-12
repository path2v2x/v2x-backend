import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/svelte';

const hlsMocks = vi.hoisted(() => ({
	attachMedia: vi.fn(),
	destroy: vi.fn(),
	loadSource: vi.fn()
}));

const apiMocks = vi.hoisted(() => ({
	fetchVideoSession: vi.fn()
}));

vi.mock('hls.js', () => ({
	default: class MockHls {
		static isSupported() {
			return true;
		}

		attachMedia = hlsMocks.attachMedia;
		destroy = hlsMocks.destroy;
		loadSource = hlsMocks.loadSource;
	}
}));

vi.mock('$lib/api', () => ({
	fetchVideoSession: apiMocks.fetchVideoSession
}));

import LiveVideoCard from '$lib/components/LiveVideoCard.svelte';

beforeEach(() => {
	vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
	vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
	vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
	hlsMocks.attachMedia.mockReset();
	hlsMocks.destroy.mockReset();
	hlsMocks.loadSource.mockReset();
	apiMocks.fetchVideoSession.mockReset();
});

afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

describe('LiveVideoCard camera selection', () => {
	it('reconnects the image stream when the selected camera changes', async () => {
		const view = render(LiveVideoCard, {
			props: {
				cameraId: 'ch1',
				streamUrl: 'https://perception.example.test/streams/ch1.mjpg',
				sourceLabel: 'Perception'
			}
		});

		await waitFor(() =>
			expect(screen.getByRole('img', { name: 'ch1 perception stream' })).toHaveAttribute(
				'src',
				'https://perception.example.test/streams/ch1.mjpg'
			)
		);

		await view.rerender({
			cameraId: 'ch4',
			streamUrl: 'https://perception.example.test/streams/ch4.mjpg',
			sourceLabel: 'Perception'
		});

		await waitFor(() =>
			expect(screen.getByRole('img', { name: 'ch4 perception stream' })).toHaveAttribute(
				'src',
				'https://perception.example.test/streams/ch4.mjpg'
			)
		);
		expect(screen.queryByRole('img', { name: 'ch1 perception stream' })).not.toBeInTheDocument();
	});

	it('mounts a video element before switching from MJPEG to HLS', async () => {
		const view = render(LiveVideoCard, {
			props: {
				cameraId: 'ch1',
				streamUrl: 'https://perception.example.test/streams/ch1.mjpg'
			}
		});
		await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument());

		await view.rerender({
			cameraId: 'ch4',
			streamUrl: 'https://video.example.test/streams/ch4.m3u8'
		});

		await waitFor(() =>
			expect(hlsMocks.loadSource).toHaveBeenCalledWith(
				'https://video.example.test/streams/ch4.m3u8'
			)
		);
		expect(hlsMocks.attachMedia).toHaveBeenCalledWith(expect.any(HTMLVideoElement));
		expect(screen.queryByRole('img')).not.toBeInTheDocument();
	});

	it('prefers hls.js when Chromium also claims native HLS support', async () => {
		vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('probably');

		render(LiveVideoCard, {
			props: {
				cameraId: 'ch2',
				streamUrl: 'https://video.example.test/streams/ch2.m3u8'
			}
		});

		await waitFor(() =>
			expect(hlsMocks.loadSource).toHaveBeenCalledWith(
				'https://video.example.test/streams/ch2.m3u8'
			)
		);
		expect(hlsMocks.attachMedia).toHaveBeenCalledWith(expect.any(HTMLVideoElement));
	});

	it('keeps the active stream alive until a renewed session is already playing', async () => {
		vi.useFakeTimers();
		apiMocks.fetchVideoSession
			.mockResolvedValueOnce({
				cameraId: 'ch1',
				streamName: 'camera-ch1',
				playbackMode: 'LIVE',
				hlsUrl: 'https://video.example.test/session-1.m3u8',
				expiresIn: 51,
				region: 'us-west-1'
			})
			.mockResolvedValueOnce({
				cameraId: 'ch1',
				streamName: 'camera-ch1',
				playbackMode: 'LIVE',
				hlsUrl: 'https://video.example.test/session-2.m3u8',
				expiresIn: 51,
				region: 'us-west-1'
			});

		let releaseRenewal!: () => void;
		const play = vi
			.spyOn(HTMLMediaElement.prototype, 'play')
			.mockResolvedValueOnce(undefined)
			.mockImplementationOnce(
				() => new Promise<void>((resolve) => (releaseRenewal = resolve))
			);

		render(LiveVideoCard, { props: { cameraId: 'ch1' } });
		await vi.advanceTimersByTimeAsync(0);
		expect(apiMocks.fetchVideoSession).toHaveBeenCalledTimes(1);
		expect(hlsMocks.loadSource).toHaveBeenCalledWith(
			'https://video.example.test/session-1.m3u8'
		);

		await vi.advanceTimersByTimeAsync(6_000);
		expect(apiMocks.fetchVideoSession).toHaveBeenCalledTimes(2);
		expect(hlsMocks.loadSource).toHaveBeenCalledWith(
			'https://video.example.test/session-2.m3u8'
		);
		expect(play).toHaveBeenCalledTimes(2);
		expect(hlsMocks.destroy).not.toHaveBeenCalled();

		releaseRenewal();
		await vi.advanceTimersByTimeAsync(0);
		expect(hlsMocks.destroy).not.toHaveBeenCalled();
		await vi.advanceTimersByTimeAsync(200);
		expect(hlsMocks.destroy).toHaveBeenCalledTimes(1);
		vi.useRealTimers();
	});
});
