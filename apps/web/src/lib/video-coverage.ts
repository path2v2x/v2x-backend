import { mergeCoverageIntervals, toIsoMillis } from './timeline';
import type { VideoCoverage } from './types';

export type CoverageFetcher = (
	cameraId: string,
	window: { start: string; end: string }
) => Promise<VideoCoverage>;

/**
 * Fetch one camera's coverage chunks sequentially. Kinesis Video Streams can
 * reject concurrent ListFragments calls. The timeline caller also serializes
 * cameras because the observed connection limit is account-wide.
 */
export async function fetchCameraCoverageSequentially(
	cameraId: string,
	startMs: number,
	endMs: number,
	chunkMs: number,
	fetcher: CoverageFetcher
): Promise<VideoCoverage> {
	if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs) {
		throw new Error('coverage range must be finite and increasing');
	}
	if (!Number.isFinite(chunkMs) || chunkMs <= 0) {
		throw new Error('coverage chunk size must be positive');
	}

	const parts: PromiseSettledResult<VideoCoverage>[] = [];
	for (let chunkStart = startMs; chunkStart < endMs; chunkStart += chunkMs) {
		const window = {
			start: toIsoMillis(chunkStart),
			end: toIsoMillis(Math.min(chunkStart + chunkMs, endMs))
		};
		try {
			parts.push({ status: 'fulfilled', value: await fetcher(cameraId, window) });
		} catch (reason) {
			parts.push({ status: 'rejected', reason });
		}
	}

	return {
		cameraId,
		start: toIsoMillis(startMs),
		end: toIsoMillis(endMs),
		intervals: mergeCoverageIntervals(
			parts.flatMap((part) => (part.status === 'fulfilled' ? part.value.intervals : []))
		),
		fragmentCount: parts.reduce(
			(sum, part) => sum + (part.status === 'fulfilled' ? part.value.fragmentCount : 0),
			0
		),
		truncated: parts.some(
			(part) => part.status === 'rejected' || (part.status === 'fulfilled' && part.value.truncated)
		)
	};
}
