const UNIX_MILLISECONDS_THRESHOLD = 1_000_000_000_000;

/** Parse producer-owned timestamps without substituting browser receipt time. */
export function producerEpochMs(value: string | number | null | undefined): number | null {
	if (value == null || value === '') return null;
	let epochMs: number;
	if (typeof value === 'number') {
		epochMs = value < UNIX_MILLISECONDS_THRESHOLD ? value * 1000 : value;
	} else {
		const trimmed = value.trim();
		if (!trimmed) return null;
		if (/^\d+(?:\.\d+)?$/.test(trimmed)) {
			const numeric = Number(trimmed);
			epochMs = numeric < UNIX_MILLISECONDS_THRESHOLD ? numeric * 1000 : numeric;
		} else {
			epochMs = Date.parse(trimmed);
		}
	}
	return Number.isFinite(epochMs) && epochMs >= 0 ? epochMs : null;
}

export function normalizeProducerTimestamp(
	value: string | number | null | undefined
): string | null {
	const epochMs = producerEpochMs(value);
	return epochMs === null ? null : new Date(epochMs).toISOString();
}

export function isProducerTimestampFresh(
	value: string | number | null | undefined,
	staleAfterMs: number,
	nowMs = Date.now(),
	futureToleranceMs = 5_000
): boolean {
	const epochMs = producerEpochMs(value);
	if (epochMs === null) return false;
	const ageMs = nowMs - epochMs;
	return ageMs >= -futureToleranceMs && ageMs <= staleAfterMs;
}

/** Return the newest valid timestamp in canonical ISO form. */
export function latestProducerTimestamp(
	values: Array<string | number | null | undefined>
): string | null {
	let latest: number | null = null;
	for (const value of values) {
		const epochMs = producerEpochMs(value);
		if (epochMs !== null && (latest === null || epochMs > latest)) latest = epochMs;
	}
	return latest === null ? null : new Date(latest).toISOString();
}
