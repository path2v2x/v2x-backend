/**
 * Translate the various warning sources into a single DashboardWarning
 * list consumed by WarningStack.
 *
 * Sources covered:
 *  - V2X alerts from telemetry (EVA firetruck, V2X signals)
 *  - V2X zone proximity (zones the ego is currently inside)
 *  - Scenario verdict (XoscFinishedEvent) — shown for VERDICT_TTL_MS
 */

import type { V2xAlert, V2xZone, XoscFinishedEvent } from '$lib/types';
import type {
	DashboardWarning,
	WarningSeverity,
	WarningSource,
} from './WarningStack.svelte';

/** How long a scenario verdict stays in the warning stack after the
 * scenario finished, in milliseconds. */
export const VERDICT_TTL_MS = 15_000;

/** Map the bridge's `signal_type` to Tesla cluster severity. */
export function mapSignalType(
	t: 'warning' | 'info' | 'alert' | string
): WarningSeverity {
	if (t === 'alert') return 'critical';
	if (t === 'warning') return 'warning';
	return 'info';
}

/** EVA firetruck alerts use a specific phrasing — distinguish them
 * from generic V2X signals so the source tag is meaningful. */
export function classifyV2xSource(message: string): WarningSource {
	const lower = message.toLowerCase();
	if (lower.includes('firetruck') || lower.includes('emergency')) {
		return 'eva';
	}
	return 'v2x';
}

export interface BuildWarningsInput {
	v2xAlerts: V2xAlert[];
	activeZoneAlerts: { zone: V2xZone }[];
	xoscLastResult: XoscFinishedEvent | null;
	/** ms timestamp of when xoscLastResult was set (for TTL filtering). */
	xoscResultSetAt: number | null;
	/** Current time in ms. Used for fade decisions in downstream stack. */
	now: number;
}

export function buildDashboardWarnings(
	input: BuildWarningsInput
): DashboardWarning[] {
	const out: DashboardWarning[] = [];

	// V2X alerts from telemetry (EVA firetruck, V2X signals).
	for (const alert of input.v2xAlerts) {
		const lastSeen =
			(alert as V2xAlert & { _lastSeen?: number })._lastSeen ?? input.now;
		out.push({
			id: `v2x-${alert.id}`,
			message: alert.message,
			severity: mapSignalType(alert.signal_type),
			source: classifyV2xSource(alert.message),
			lastUpdate: lastSeen,
			detail:
				typeof alert.distance === 'number'
					? `${alert.distance.toFixed(1)}m`
					: undefined,
		});
	}

	// Active V2X zone proximity (zones the car is currently inside).
	for (const za of input.activeZoneAlerts) {
		const z = za.zone;
		out.push({
			id: `zone-${z.id}`,
			message: z.message,
			severity: mapSignalType(z.signal_type),
			source: 'v2x',
			lastUpdate: input.now,
		});
	}

	// Scenario verdict — short-lived, fixed TTL.
	if (input.xoscLastResult && input.xoscResultSetAt != null) {
		const age = input.now - input.xoscResultSetAt;
		if (age < VERDICT_TTL_MS) {
			const r = input.xoscLastResult;
			const failed = r.verdict === 'FAILURE';
			const fileLabel = r.file ? `: ${r.file}` : '';
			out.push({
				id: `verdict-${input.xoscResultSetAt}`,
				message: `Scenario ${failed ? 'failed' : 'completed'}${fileLabel}`,
				severity: failed ? 'critical' : 'info',
				source: 'verdict',
				lastUpdate: input.now, // keep alive for the whole TTL
				detail:
					typeof r.duration_sec === 'number'
						? `${r.duration_sec.toFixed(1)}s`
						: undefined,
			});
		}
	}

	return out;
}
