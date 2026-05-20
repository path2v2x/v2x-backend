import { describe, it, expect } from 'vitest';
import {
	buildDashboardWarnings,
	mapSignalType,
	classifyV2xSource,
	perceptionDetectionToWarning,
	VERDICT_TTL_MS,
	PERCEPTION_THRESHOLDS,
} from '$lib/components/dashboard/warnings';
import type { Detection, V2xAlert, V2xZone, XoscFinishedEvent } from '$lib/types';

function mkDet(overrides: Partial<Detection> = {}): Detection {
	return {
		id: 'vehicle-0',
		class: 'vehicle',
		pos: [10, 0],
		distance: 10,
		bbox_dim: [4.5, 1.8],
		in_path: true,
		alert: 'none',
		...overrides,
	};
}

describe('mapSignalType', () => {
	it('maps alert to critical', () => {
		expect(mapSignalType('alert')).toBe('critical');
	});
	it('maps warning to warning', () => {
		expect(mapSignalType('warning')).toBe('warning');
	});
	it('maps info (and anything else) to info', () => {
		expect(mapSignalType('info')).toBe('info');
		expect(mapSignalType('unknown')).toBe('info');
	});
});

describe('classifyV2xSource', () => {
	it('flags firetruck messages as eva', () => {
		expect(classifyV2xSource('Firetruck approaching from behind')).toBe('eva');
		expect(classifyV2xSource('firetruck')).toBe('eva');
	});
	it('flags emergency keyword as eva', () => {
		expect(classifyV2xSource('Emergency vehicle')).toBe('eva');
	});
	it('defaults to v2x for generic messages', () => {
		expect(classifyV2xSource('Construction zone ahead')).toBe('v2x');
		expect(classifyV2xSource('Speed limit reduced')).toBe('v2x');
	});
});

function mkAlert(overrides: Partial<V2xAlert> = {}): V2xAlert {
	return {
		id: 1,
		message: 'Test alert',
		signal_type: 'warning',
		distance: 10,
		...overrides,
	};
}

function mkZone(overrides: Partial<V2xZone> = {}): V2xZone {
	return {
		id: 'z1',
		name: 'Test Zone',
		message: 'Zone alert',
		signal_type: 'warning',
		polygon: [
			[0, 0],
			[1, 0],
			[1, 1],
		],
		color: '#fff',
		...overrides,
	};
}

describe('buildDashboardWarnings', () => {
	it('returns empty for empty inputs', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 1000,
		});
		expect(out).toEqual([]);
	});

	it('maps a V2xAlert to a DashboardWarning with correct id prefix and severity', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [mkAlert({ id: 7, signal_type: 'alert', message: 'Firetruck approaching' })],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 1000,
		});
		expect(out).toHaveLength(1);
		expect(out[0].id).toBe('v2x-7');
		expect(out[0].severity).toBe('critical');
		expect(out[0].source).toBe('eva');
		expect(out[0].detail).toBe('10.0m');
	});

	it('uses _lastSeen for lastUpdate when present', () => {
		const alert = mkAlert({ id: 3 }) as V2xAlert & { _lastSeen?: number };
		alert._lastSeen = 500;
		const out = buildDashboardWarnings({
			v2xAlerts: [alert],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 1000,
		});
		expect(out[0].lastUpdate).toBe(500);
	});

	it('falls back to now when _lastSeen is missing', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [mkAlert({ id: 4 })],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 2000,
		});
		expect(out[0].lastUpdate).toBe(2000);
	});

	it('omits detail when distance is missing', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [
				mkAlert({ id: 1, distance: undefined as unknown as number }),
			],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 1000,
		});
		expect(out[0].detail).toBeUndefined();
	});

	it('maps an active zone alert', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [],
			activeZoneAlerts: [{ zone: mkZone({ id: 'school', signal_type: 'alert' }) }],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 1000,
		});
		expect(out).toHaveLength(1);
		expect(out[0].id).toBe('zone-school');
		expect(out[0].severity).toBe('critical');
		expect(out[0].source).toBe('v2x');
		expect(out[0].lastUpdate).toBe(1000);
	});

	it('includes the scenario verdict within TTL', () => {
		const verdict: XoscFinishedEvent = {
			file: 'firetruck.xosc',
			exit_code: 0,
			verdict: 'SUCCESS',
			duration_sec: 42.5,
		};
		const out = buildDashboardWarnings({
			v2xAlerts: [],
			activeZoneAlerts: [],
			xoscLastResult: verdict,
			xoscResultSetAt: 1000,
			now: 1500, // 500ms after set; within 15s TTL
		});
		expect(out).toHaveLength(1);
		expect(out[0].id).toBe('verdict-1000');
		expect(out[0].severity).toBe('info');
		expect(out[0].source).toBe('verdict');
		expect(out[0].message).toContain('completed');
		expect(out[0].message).toContain('firetruck.xosc');
		expect(out[0].detail).toBe('42.5s');
	});

	it('drops the scenario verdict past TTL', () => {
		const verdict: XoscFinishedEvent = {
			file: 'sample.xosc',
			exit_code: 1,
			verdict: 'FAILURE',
			duration_sec: 10,
		};
		const out = buildDashboardWarnings({
			v2xAlerts: [],
			activeZoneAlerts: [],
			xoscLastResult: verdict,
			xoscResultSetAt: 0,
			now: VERDICT_TTL_MS + 1000,
		});
		expect(out).toEqual([]);
	});

	it('marks FAILURE verdict as critical', () => {
		const verdict: XoscFinishedEvent = {
			file: null,
			exit_code: 1,
			verdict: 'FAILURE',
			duration_sec: 5,
		};
		const out = buildDashboardWarnings({
			v2xAlerts: [],
			activeZoneAlerts: [],
			xoscLastResult: verdict,
			xoscResultSetAt: 1000,
			now: 1100,
		});
		expect(out).toHaveLength(1);
		expect(out[0].severity).toBe('critical');
		expect(out[0].message).toContain('failed');
	});

	it('combines all sources into one ordered list (preserving insertion order)', () => {
		const verdict: XoscFinishedEvent = {
			file: 'a.xosc',
			exit_code: 0,
			verdict: 'SUCCESS',
			duration_sec: 12,
		};
		const out = buildDashboardWarnings({
			v2xAlerts: [mkAlert({ id: 1 }), mkAlert({ id: 2 })],
			activeZoneAlerts: [{ zone: mkZone({ id: 'z1' }) }],
			xoscLastResult: verdict,
			xoscResultSetAt: 100,
			now: 200,
		});
		expect(out).toHaveLength(4);
		expect(out.map((w) => w.id)).toEqual([
			'v2x-1',
			'v2x-2',
			'zone-z1',
			'verdict-100',
		]);
	});

	it('handles a non-firetruck v2x alert correctly (source = v2x)', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [mkAlert({ id: 9, message: 'Construction zone ahead' })],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 0,
		});
		expect(out[0].source).toBe('v2x');
	});
});

describe('perceptionDetectionToWarning', () => {
	it('promotes a pedestrian in path within threshold to critical', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'pedestrian', in_path: true, distance: 12 }),
			1000
		);
		expect(w).not.toBeNull();
		expect(w!.severity).toBe('critical');
		expect(w!.source).toBe('perception');
		expect(w!.message).toContain('Pedestrian');
		expect(w!.detail).toBe('12.0m');
	});

	it('drops a pedestrian off the path', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'pedestrian', in_path: false, distance: 5 }),
			1000
		);
		expect(w).toBeNull();
	});

	it('drops a pedestrian beyond pedestrianInPathM', () => {
		const w = perceptionDetectionToWarning(
			mkDet({
				class: 'pedestrian',
				in_path: true,
				distance: PERCEPTION_THRESHOLDS.pedestrianInPathM + 1,
			}),
			1000
		);
		expect(w).toBeNull();
	});

	it('promotes a close vehicle in path to critical', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'vehicle', in_path: true, distance: 10 }),
			1000
		);
		expect(w!.severity).toBe('critical');
		expect(w!.message).toContain('Vehicle close');
	});

	it('promotes a farther vehicle in path to warning', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'vehicle', in_path: true, distance: 30 }),
			1000
		);
		expect(w!.severity).toBe('warning');
		expect(w!.message).toBe('Vehicle ahead');
	});

	it('drops a vehicle off path', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'vehicle', in_path: false, distance: 10 }),
			1000
		);
		expect(w).toBeNull();
	});

	it('promotes a cone in path to warning', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'cone', in_path: true, distance: 8 }),
			1000
		);
		expect(w!.severity).toBe('warning');
		expect(w!.message).toBe('Obstacle in path');
	});

	it('drops a cone far from the ego', () => {
		const w = perceptionDetectionToWarning(
			mkDet({
				class: 'cone',
				in_path: true,
				distance: PERCEPTION_THRESHOLDS.coneInPathM + 5,
			}),
			1000
		);
		expect(w).toBeNull();
	});

	it('promotes a traffic light in path to info', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'traffic_light', in_path: true, distance: 20 }),
			1000
		);
		expect(w!.severity).toBe('info');
	});

	it('ignores traffic signs (visual only, no warning card)', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ class: 'traffic_sign', in_path: true, distance: 10 }),
			1000
		);
		expect(w).toBeNull();
	});

	it('uses stable id prefix so cards dedup across ticks', () => {
		const w = perceptionDetectionToWarning(
			mkDet({ id: 'vehicle-42', class: 'vehicle', in_path: true, distance: 10 }),
			1234
		);
		expect(w!.id).toBe('perception-vehicle-42');
		expect(w!.lastUpdate).toBe(1234);
	});
});

describe('buildDashboardWarnings with perception inputs', () => {
	it('includes detection-derived warnings alongside V2X / verdict', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 100,
			detections: [
				mkDet({ id: 'p1', class: 'pedestrian', in_path: true, distance: 8 }),
				mkDet({ id: 'v1', class: 'vehicle', in_path: true, distance: 11 }),
				mkDet({ id: 'v2', class: 'vehicle', in_path: false, distance: 5 }),
			],
		});
		const ids = out.map((w) => w.id);
		expect(ids).toContain('perception-p1');
		expect(ids).toContain('perception-v1');
		expect(ids).not.toContain('perception-v2'); // off-path vehicle dropped
	});

	it('handles missing detections array gracefully', () => {
		const out = buildDashboardWarnings({
			v2xAlerts: [],
			activeZoneAlerts: [],
			xoscLastResult: null,
			xoscResultSetAt: null,
			now: 0,
		});
		expect(out).toEqual([]);
	});
});
