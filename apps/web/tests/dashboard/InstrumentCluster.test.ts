import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import InstrumentCluster from '$lib/components/dashboard/InstrumentCluster.svelte';

describe('InstrumentCluster', () => {
	it('mounts and renders gauge, steering bar, and brake bar', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 50, gear: 'D', throttle: 0.3, brake: 0, steer: 0 },
		});
		expect(getByTestId('instrument-cluster')).toBeInTheDocument();
		expect(getByTestId('throttle-gauge')).toBeInTheDocument();
		expect(getByTestId('gauge-speed')).toBeInTheDocument();
		expect(getByTestId('gauge-gear')).toBeInTheDocument();
		expect(getByTestId('steering-bar')).toBeInTheDocument();
		expect(getByTestId('brake-bar')).toBeInTheDocument();
	});

	it('shows the converted speed in the gauge centre (50 km/h ≈ 31 mph)', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 50, gear: 'D', throttle: 0, brake: 0, steer: 0 },
		});
		expect(getByTestId('gauge-speed').textContent).toBe('31');
	});

	it('shows the active gear letter in the gauge centre', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'R', throttle: 0, brake: 0.5, steer: 0 },
		});
		const letter = getByTestId('gauge-gear');
		expect(letter.dataset.gear).toBe('R');
		expect(letter.textContent).toBe('R');
	});

	it('centers steering dot at zero steer', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, brake: 0, steer: 0 },
		});
		expect(getByTestId('steering-dot').dataset.pct).toBe('50.0');
	});

	it('moves steering dot left when steer is negative', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, brake: 0, steer: -0.5 },
		});
		expect(getByTestId('steering-dot').dataset.pct).toBe('25.0');
	});

	it('moves steering dot right when steer is positive', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, brake: 0, steer: 0.5 },
		});
		expect(getByTestId('steering-dot').dataset.pct).toBe('75.0');
	});

	it('reflects throttle on the gauge fill', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0.7, brake: 0, steer: 0 },
		});
		expect(getByTestId('throttle-gauge').dataset.fill).toBe('0.700');
	});

	it('reflects brake as bar fill', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, brake: 0.4, steer: 0 },
		});
		expect(getByTestId('brake-bar').dataset.fill).toBe('0.400');
	});
});
