import { describe, expect, it } from 'vitest';

import {
	ARCHIVE_MAX_CURSOR_DRIFT_MS,
	archiveCursorNeedsCorrection,
	archiveMediaTimeForEpoch,
	layoutMarkers
} from '../../src/lib/timeline';

describe('archive clock synchronisation', () => {
	it('maps wall time through fragment program-date-time when available', () => {
		expect(archiveMediaTimeForEpoch(10_750, 10_000, 5_000)).toBe(0.75);
	});

	it('uses the playback-window base only until a fragment PDT arrives', () => {
		expect(archiveMediaTimeForEpoch(10_750, null, 10_000)).toBe(0.75);
		expect(archiveMediaTimeForEpoch(9_000, null, 10_000)).toBe(0);
	});

	it('corrects drift only when it exceeds the strict replay tolerance', () => {
		expect(archiveCursorNeedsCorrection(10_000, 10_000 + ARCHIVE_MAX_CURSOR_DRIFT_MS)).toBe(
			false
		);
		expect(
			archiveCursorNeedsCorrection(10_000, 10_001 + ARCHIVE_MAX_CURSOR_DRIFT_MS)
		).toBe(true);
		expect(
			archiveCursorNeedsCorrection(10_000, 9_999 - ARCHIVE_MAX_CURSOR_DRIFT_MS)
		).toBe(true);
	});

	it('never seeks from invalid clock values', () => {
		expect(archiveCursorNeedsCorrection(Number.NaN, 10_000)).toBe(false);
		expect(archiveCursorNeedsCorrection(10_000, Number.POSITIVE_INFINITY)).toBe(false);
	});

	it('visually quarantines legacy receipt-time markers', () => {
		const base = {
			object_id: 'legacy-car',
			object_type: 'car',
			device_id: 'ch1',
			first_seen: '2026-07-10T03:57:23.000Z',
			last_seen: '2026-07-10T03:57:24.000Z',
			count: 2,
			max_confidence: 0.9
		};
		const start = Date.parse('2026-07-10T03:57:20.000Z');
		const end = Date.parse('2026-07-10T03:57:30.000Z');
		expect(layoutMarkers([base], start, end)[0].color).toBe('#64748b');
		expect(
			layoutMarkers([{ ...base, media_time_trusted: true }], start, end)[0].color
		).not.toBe('#64748b');
	});
});
