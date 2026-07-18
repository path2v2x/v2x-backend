import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import InstrumentCluster from '$lib/components/dashboard/InstrumentCluster.svelte';

describe('InstrumentCluster', () => {
	it('mounts and renders gauge, steering path, and the vertical throttle/brake bars', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 50, gear: 'D', throttle: 0.3, brake: 0, steer: 0 },
		});
		expect(getByTestId('instrument-cluster')).toBeInTheDocument();
		expect(getByTestId('throttle-gauge')).toBeInTheDocument();
		expect(getByTestId('gauge-speed')).toBeInTheDocument();
		expect(getByTestId('gauge-gear')).toBeInTheDocument();
		expect(getByTestId('steering-path')).toBeInTheDocument();
		// Vertical throttle/brake bars restored by user request (2026-07-15).
		expect(getByTestId('throttle-bar')).toBeInTheDocument();
		expect(getByTestId('brake-bar')).toBeInTheDocument();
	});

	it('reflects throttle as bar fill', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0.7, brake: 0, steer: 0 },
		});
		expect(getByTestId('throttle-bar').dataset.fill).toBe('0.700');
	});

	it('reflects brake as bar fill', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, brake: 0.4, steer: 0 },
		});
		expect(getByTestId('brake-bar').dataset.fill).toBe('0.400');
	});

	it('clamps out-of-range throttle/brake on the bars', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 1.5, brake: -0.2, steer: 0 },
		});
		expect(getByTestId('throttle-bar').dataset.fill).toBe('1.000');
		expect(getByTestId('brake-bar').dataset.fill).toBe('0.000');
	});

	it('shows the converted speed in the gauge centre (50 km/h ≈ 31 mph)', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 50, gear: 'D', throttle: 0, steer: 0 },
		});
		expect(getByTestId('gauge-speed').textContent).toBe('31');
	});

	it('shows the active gear letter in the gauge centre', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'R', throttle: 0, steer: 0 },
		});
		const letter = getByTestId('gauge-gear');
		expect(letter.dataset.gear).toBe('R');
		expect(letter.textContent).toBe('R');
	});

	it('exposes the clamped steer value on the steering path', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, steer: 0 },
		});
		expect(getByTestId('steering-path').dataset.steer).toBe('0.000');
	});

	it('reflects negative steer (left turn)', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, steer: -0.5 },
		});
		expect(getByTestId('steering-path').dataset.steer).toBe('-0.500');
	});

	it('reflects positive steer (right turn)', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, steer: 0.5 },
		});
		expect(getByTestId('steering-path').dataset.steer).toBe('0.500');
	});

	it('clamps extreme steer values', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0, steer: 2 },
		});
		expect(getByTestId('steering-path').dataset.steer).toBe('1.000');
	});

	it('reflects throttle on the gauge fill', () => {
		const { getByTestId } = render(InstrumentCluster, {
			props: { speed: 0, gear: 'D', throttle: 0.7, steer: 0 },
		});
		expect(getByTestId('throttle-gauge').dataset.fill).toBe('0.700');
	});

});
