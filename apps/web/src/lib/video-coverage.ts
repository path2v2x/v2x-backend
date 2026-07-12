import { mergeCoverageIntervals, toIsoMillis } from './timeline';
import type { VideoCoverage } from './types';

export type CoverageFetcher = (
	cameraId: string,
	window: { start: string; end: string }
) => Promise<VideoCoverage>;

const IMMUTABLE_LAG_MS = 2 * 60 * 1000;
const completeChunkCache = new Map<string, VideoCoverage>();

export function clearCoverageChunkCache(): void {
	completeChunkCache.clear();
}

function chunkCacheKey(cameraId: string, startMs: number, endMs: number): string {
	return `${cameraId}:${startMs}:${endMs}`;
}

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

	// Align requests to stable boundaries. Otherwise every rolling refresh moves
	// all 24 query windows and defeats caching even though historical fragments
	// are immutable. Only the chunk touching the live edge is re-fetched.
	const alignedStartMs = Math.floor(startMs / chunkMs) * chunkMs;
	const alignedEndMs = Math.ceil(endMs / chunkMs) * chunkMs;
	const immutableBeforeMs = Date.now() - IMMUTABLE_LAG_MS;
	const parts: PromiseSettledResult<VideoCoverage>[] = [];
	for (let chunkStart = alignedStartMs; chunkStart < alignedEndMs; chunkStart += chunkMs) {
		const chunkEnd = Math.min(chunkStart + chunkMs, alignedEndMs);
		const cacheKey = chunkCacheKey(cameraId, chunkStart, chunkEnd);
		const cached = completeChunkCache.get(cacheKey);
		if (cached) {
			parts.push({ status: 'fulfilled', value: cached });
			continue;
		}
		const window = {
			start: toIsoMillis(chunkStart),
			end: toIsoMillis(chunkEnd)
		};
		try {
			const value = await fetcher(cameraId, window);
			parts.push({ status: 'fulfilled', value });
			if (chunkEnd <= immutableBeforeMs && value.truncated !== true) {
				completeChunkCache.set(cacheKey, value);
			}
		} catch (reason) {
			parts.push({ status: 'rejected', reason });
		}
	}

	return {
		cameraId,
		start: toIsoMillis(startMs),
		end: toIsoMillis(endMs),
		intervals: mergeCoverageIntervals(
			parts
				.flatMap((part) => (part.status === 'fulfilled' ? part.value.intervals : []))
				.flatMap((interval) => {
					const parsedStart = Date.parse(interval.start);
					const parsedEnd = Date.parse(interval.end);
					if (!Number.isFinite(parsedStart) || !Number.isFinite(parsedEnd)) return [];
					const clippedStart = Math.max(parsedStart, startMs);
					const clippedEnd = Math.min(parsedEnd, endMs);
					return clippedStart < clippedEnd
						? [{ start: toIsoMillis(clippedStart), end: toIsoMillis(clippedEnd) }]
						: [];
				})
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
