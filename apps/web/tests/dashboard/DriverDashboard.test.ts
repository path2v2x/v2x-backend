import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import DriverDashboard, {
	gearFromCarla,
} from '$lib/components/dashboard/DriverDashboard.svelte';
import type { VehicleTelemetry } from '$lib/types';

function mkTelemetry(overrides: Partial<VehicleTelemetry> = {}): VehicleTelemetry {
	return {
		speed: 0,
		gear: 1,
		pos: [0, 0, 0],
		rot: [0, 0, 0],
		steer: 0,
		throttle: 0,
		brake: 0,
		nearby_actors: [],
		...overrides,
	};
}

describe('gearFromCarla', () => {
	it('maps positive ints to D', () => {
		expect(gearFromCarla(1)).toBe('D');
		expect(gearFromCarla(3)).toBe('D');
	});

	it('maps zero to N', () => {
		expect(gearFromCarla(0)).toBe('N');
	});

	it('maps negative ints to R', () => {
		expect(gearFromCarla(-1)).toBe('R');
		expect(gearFromCarla(-2)).toBe('R');
	});
});

describe('DriverDashboard', () => {
	it('mounts even with null telemetry', () => {
		const { getByTestId } = render(DriverDashboard, {
			props: { telemetry: null, warnings: [], now: 0 },
		});
		expect(getByTestId('driver-dashboard')).toBeInTheDocument();
		expect(getByTestId('instrument-cluster')).toBeInTheDocument();
		expect(getByTestId('center-stack')).toBeInTheDocument();
	});

	it('shows 0 mph with null telemetry', () => {
		const { getByTestId } = render(DriverDashboard, {
			props: { telemetry: null, now: 0 },
		});
		expect(getByTestId('speed-value').textContent).toBe('0');
	});

	it('renders speed from telemetry (60 km/h ≈ 37 mph)', () => {
		const { getByTestId } = render(DriverDashboard, {
			props: { telemetry: mkTelemetry({ speed: 60, gear: 1 }), now: 0 },
		});
		expect(getByTestId('speed-value').textContent).toBe('37');
	});

	it('renders D when gear is positive', () => {
		const { getByTestId } = render(DriverDashboard, {
			props: { telemetry: mkTelemetry({ gear: 2 }), now: 0 },
		});
		expect(getByTestId('gear-letter').dataset.gear).toBe('D');
	});

	it('renders R when gear is negative', () => {
		const { getByTestId } = render(DriverDashboard, {
			props: { telemetry: mkTelemetry({ gear: -1 }), now: 0 },
		});
		expect(getByTestId('gear-letter').dataset.gear).toBe('R');
	});

	it('renders N when gear is zero', () => {
		const { getByTestId } = render(DriverDashboard, {
			props: { telemetry: mkTelemetry({ gear: 0 }), now: 0 },
		});
		expect(getByTestId('gear-letter').dataset.gear).toBe('N');
	});

	// Vehicle-visualization removed by user request — the right side of
	// the dashboard is now reserved for messages only.

	it('shows warnings in the center stack', () => {
		const { getByTestId } = render(DriverDashboard, {
			props: {
				telemetry: mkTelemetry(),
				warnings: [
					{
						id: 'eva1',
						message: 'Firetruck',
						severity: 'critical',
						source: 'eva',
						lastUpdate: 1000,
					},
				],
				now: 1000,
			},
		});
		expect(getByTestId('warning-eva1')).toBeInTheDocument();
	});

	it('fades stale warnings based on the now prop', () => {
		const { queryByTestId } = render(DriverDashboard, {
			props: {
				telemetry: mkTelemetry(),
				warnings: [
					{
						id: 'stale',
						message: 'Old',
						severity: 'info',
						source: 'scenario',
						lastUpdate: 1000,
					},
				],
				now: 5000,
			},
		});
		expect(queryByTestId('warning-stale')).toBeNull();
	});
});
