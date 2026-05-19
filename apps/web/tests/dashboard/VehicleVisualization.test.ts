import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import VehicleVisualization from '$lib/components/dashboard/VehicleVisualization.svelte';

describe('VehicleVisualization', () => {
	it('renders the ego car centered when no nearby actors', () => {
		const { getByTestId, queryByTestId } = render(VehicleVisualization, {
			props: { egoPos: [0, 0], egoYaw: 0, steer: 0, nearby: [] },
		});
		expect(getByTestId('ego-car')).toBeInTheDocument();
		expect(getByTestId('ego-wheel-left')).toBeInTheDocument();
		expect(getByTestId('ego-wheel-right')).toBeInTheDocument();
	});

	it('hides actors outside the visibility radius', () => {
		const { queryByTestId } = render(VehicleVisualization, {
			props: {
				egoPos: [0, 0],
				egoYaw: 0,
				steer: 0,
				nearby: [{ id: 99, pos: [200, 200], yaw: 0 }],
				radiusM: 30,
			},
		});
		expect(queryByTestId('nearby-99')).toBeNull();
	});

	it('renders an actor inside the visibility radius', () => {
		const { getByTestId } = render(VehicleVisualization, {
			props: {
				egoPos: [0, 0],
				egoYaw: 0,
				steer: 0,
				nearby: [{ id: 42, pos: [5, 0], yaw: 0 }],
				radiusM: 30,
			},
		});
		expect(getByTestId('nearby-42')).toBeInTheDocument();
	});

	it('projects an actor directly forward of ego to above the ego on SVG (smaller y)', () => {
		const { getByTestId } = render(VehicleVisualization, {
			props: {
				egoPos: [0, 0],
				egoYaw: 0, // facing world +x
				steer: 0,
				nearby: [{ id: 1, pos: [10, 0], yaw: 0 }],
				radiusM: 30,
			},
		});
		// ego is centered at SVG (120, 90). Actor 10m forward (+x) → SVG up (smaller y).
		const g = getByTestId('nearby-1');
		const transform = g.getAttribute('transform') || '';
		const match = transform.match(/translate\(([\d.\-]+),([\d.\-]+)\)/);
		expect(match).not.toBeNull();
		const sy = parseFloat(match![2]);
		expect(sy).toBeLessThan(90);
	});

	it('projects an actor directly to ego right (yaw=0) to the right on SVG', () => {
		const { getByTestId } = render(VehicleVisualization, {
			props: {
				egoPos: [0, 0],
				egoYaw: 0,
				steer: 0,
				nearby: [{ id: 1, pos: [0, 10], yaw: 0 }],
				radiusM: 30,
			},
		});
		const g = getByTestId('nearby-1');
		const transform = g.getAttribute('transform') || '';
		const match = transform.match(/translate\(([\d.\-]+),([\d.\-]+)\)/);
		expect(match).not.toBeNull();
		const sx = parseFloat(match![1]);
		// Ego right (+y_world when egoYaw=0) → SVG +x. Should be greater than center 120.
		expect(sx).toBeGreaterThan(120);
	});

	it('rotates front wheels with steer input', () => {
		const { getByTestId } = render(VehicleVisualization, {
			props: { egoPos: [0, 0], egoYaw: 0, steer: 0.5, nearby: [] },
		});
		const leftWheel = getByTestId('ego-wheel-left');
		const transform = leftWheel.getAttribute('transform') || '';
		// 0.5 * 28 = 14 degrees
		expect(transform).toContain('rotate(14)');
	});

	it('wheels at 0 rotation when steer is 0', () => {
		const { getByTestId } = render(VehicleVisualization, {
			props: { egoPos: [0, 0], egoYaw: 0, steer: 0, nearby: [] },
		});
		const leftWheel = getByTestId('ego-wheel-left');
		expect(leftWheel.getAttribute('transform')).toContain('rotate(0)');
	});

	it('respects egoYaw — actor 90° relative to a rotated ego maps to ego-right', () => {
		// Ego facing world +y (yaw=90), actor at world (0, 10) is now in front of ego.
		// Ego-frame forward = 10 → SVG up (smaller y than center).
		const { getByTestId } = render(VehicleVisualization, {
			props: {
				egoPos: [0, 0],
				egoYaw: 90,
				steer: 0,
				nearby: [{ id: 7, pos: [0, 10], yaw: 90 }],
				radiusM: 30,
			},
		});
		const g = getByTestId('nearby-7');
		const transform = g.getAttribute('transform') || '';
		const match = transform.match(/translate\(([\d.\-]+),([\d.\-]+)\)/);
		expect(match).not.toBeNull();
		const sy = parseFloat(match![2]);
		expect(sy).toBeLessThan(90);
	});
});
