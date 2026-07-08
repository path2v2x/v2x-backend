import type { CoverageInterval, TimelineEvent, TimelineHistogramBucket } from './types';

/** Length of one archive playback window requested from the read API. */
export const PLAYBACK_WINDOW_MS = 15 * 60 * 1000;

/** How far from the window edge the pre-fetch of the next window starts. */
export const WINDOW_EDGE_MARGIN_MS = 60 * 1000;

export const TIMELINE_SPAN_MS = 24 * 60 * 60 * 1000;

export const OBJECT_TYPE_COLORS: Record<string, string> = {
	car: '#38bdf8',
	truck: '#facc15',
	bus: '#c084fc',
	person: '#4ade80',
	default: '#f87171'
};

export function objectTypeColor(objectType: string): string {
	return OBJECT_TYPE_COLORS[objectType] ?? OBJECT_TYPE_COLORS.default;
}

export function toIsoMillis(epochMs: number): string {
	return new Date(epochMs).toISOString().replace(/(\.\d{3})\d*Z$/, '$1Z');
}

export function parseIsoMs(value: string | null | undefined): number | null {
	if (!value) return null;
	const ms = Date.parse(value);
	return Number.isNaN(ms) ? null : ms;
}

export interface PlaybackWindow {
	startMs: number;
	endMs: number;
	start: string;
	end: string;
}

/**
 * Compute the playback window containing `cursorMs`. Windows are aligned to
 * fixed boundaries so scrubbing back and forth reuses the same HLS session.
 */
export function windowForCursor(cursorMs: number, nowMs: number): PlaybackWindow {
	let startMs = Math.floor(cursorMs / PLAYBACK_WINDOW_MS) * PLAYBACK_WINDOW_MS;
	let endMs = startMs + PLAYBACK_WINDOW_MS;
	if (endMs > nowMs) {
		endMs = nowMs;
		startMs = Math.max(endMs - PLAYBACK_WINDOW_MS, nowMs - TIMELINE_SPAN_MS);
	}
	return { startMs, endMs, start: toIsoMillis(startMs), end: toIsoMillis(endMs) };
}

export interface MarkerLayout {
	event: TimelineEvent;
	x: number; // 0..1 fraction across the visible span
	color: string;
}

export function layoutMarkers(
	events: TimelineEvent[],
	viewStartMs: number,
	viewEndMs: number
): MarkerLayout[] {
	const span = viewEndMs - viewStartMs;
	if (span <= 0) return [];
	const markers: MarkerLayout[] = [];
	for (const event of events) {
		const t = parseIsoMs(event.first_seen);
		if (t === null || t < viewStartMs || t > viewEndMs) continue;
		markers.push({
			event,
			x: (t - viewStartMs) / span,
			color: objectTypeColor(event.object_type)
		});
	}
	return markers;
}

export interface CoverageSegmentLayout {
	x: number;
	width: number;
}

export function layoutCoverage(
	intervals: CoverageInterval[],
	viewStartMs: number,
	viewEndMs: number
): CoverageSegmentLayout[] {
	const span = viewEndMs - viewStartMs;
	if (span <= 0) return [];
	const segments: CoverageSegmentLayout[] = [];
	for (const interval of intervals) {
		const s = parseIsoMs(interval.start);
		const e = parseIsoMs(interval.end);
		if (s === null || e === null || e < viewStartMs || s > viewEndMs) continue;
		const clampedStart = Math.max(s, viewStartMs);
		const clampedEnd = Math.min(e, viewEndMs);
		segments.push({
			x: (clampedStart - viewStartMs) / span,
			width: Math.max((clampedEnd - clampedStart) / span, 0.0005)
		});
	}
	return segments;
}

export interface HistogramBarLayout {
	x: number;
	width: number;
	total: number;
	intensity: number; // 0..1 relative to the max bucket in view
}

export function layoutHistogram(
	buckets: TimelineHistogramBucket[],
	bucketSeconds: number,
	viewStartMs: number,
	viewEndMs: number
): HistogramBarLayout[] {
	const span = viewEndMs - viewStartMs;
	if (span <= 0) return [];
	const bucketMs = bucketSeconds * 1000;
	const visible: { x: number; width: number; total: number }[] = [];
	let max = 0;
	for (const bucket of buckets) {
		const s = parseIsoMs(bucket.bucket_start);
		if (s === null || s + bucketMs < viewStartMs || s > viewEndMs) continue;
		const total = Object.values(bucket.counts).reduce((sum, n) => sum + n, 0);
		if (total > max) max = total;
		visible.push({
			x: (Math.max(s, viewStartMs) - viewStartMs) / span,
			width: bucketMs / span,
			total
		});
	}
	if (max === 0) return [];
	return visible.map((bar) => ({ ...bar, intensity: bar.total / max }));
}

/** Merge sorted-ish coverage intervals (e.g. from chunked requests). */
export function mergeCoverageIntervals(
	intervals: CoverageInterval[],
	toleranceMs = 15_000
): CoverageInterval[] {
	const parsed = intervals
		.map((i) => ({ start: parseIsoMs(i.start), end: parseIsoMs(i.end) }))
		.filter((i): i is { start: number; end: number } => i.start !== null && i.end !== null)
		.sort((a, b) => a.start - b.start);
	const merged: { start: number; end: number }[] = [];
	for (const interval of parsed) {
		const last = merged[merged.length - 1];
		if (last && interval.start - last.end <= toleranceMs) {
			if (interval.end > last.end) last.end = interval.end;
		} else {
			merged.push({ ...interval });
		}
	}
	return merged.map((i) => ({
		start: new Date(i.start).toISOString(),
		end: new Date(i.end).toISOString()
	}));
}

export function formatClock(epochMs: number): string {
	return new Date(epochMs).toLocaleTimeString([], {
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit'
	});
}

export function formatShortClock(epochMs: number): string {
	return new Date(epochMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Evenly spaced tick marks for the visible span. */
export function timeTicks(
	viewStartMs: number,
	viewEndMs: number,
	targetCount = 8
): { x: number; label: string }[] {
	const span = viewEndMs - viewStartMs;
	if (span <= 0) return [];
	const steps = [
		60_000, 5 * 60_000, 10 * 60_000, 15 * 60_000, 30 * 60_000,
		3_600_000, 2 * 3_600_000, 3 * 3_600_000, 6 * 3_600_000, 12 * 3_600_000
	];
	const step = steps.find((s) => span / s <= targetCount) ?? steps[steps.length - 1];
	const ticks: { x: number; label: string }[] = [];
	for (let t = Math.ceil(viewStartMs / step) * step; t <= viewEndMs; t += step) {
		ticks.push({ x: (t - viewStartMs) / span, label: formatShortClock(t) });
	}
	return ticks;
}
