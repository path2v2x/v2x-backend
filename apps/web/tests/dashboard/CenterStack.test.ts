import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import CenterStack from '$lib/components/dashboard/CenterStack.svelte';

describe('CenterStack', () => {
	it('renders both viz and warning stack containers', () => {
		const { getByTestId } = render(CenterStack, {
			props: { egoPos: [0, 0], egoYaw: 0, steer: 0, nearby: [], warnings: [], now: 0 },
		});
		expect(getByTestId('center-stack')).toBeInTheDocument();
		expect(getByTestId('vehicle-viz')).toBeInTheDocument();
		expect(getByTestId('warning-stack')).toBeInTheDocument();
	});

	it('renders warnings on top of viz', () => {
		const { getByTestId } = render(CenterStack, {
			props: {
				egoPos: [0, 0],
				egoYaw: 0,
				steer: 0,
				nearby: [],
				warnings: [
					{
						id: 'eva1',
						message: 'Firetruck approaching',
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

	it('passes nearby vehicles to viz', () => {
		const { getByTestId } = render(CenterStack, {
			props: {
				egoPos: [0, 0],
				egoYaw: 0,
				steer: 0,
				nearby: [{ id: 5, pos: [3, 4], yaw: 0 }],
				warnings: [],
				now: 0,
			},
		});
		expect(getByTestId('nearby-5')).toBeInTheDocument();
	});

	it('passes steer to viz wheels', () => {
		const { getByTestId } = render(CenterStack, {
			props: { egoPos: [0, 0], egoYaw: 0, steer: 1, nearby: [], warnings: [], now: 0 },
		});
		// 1 * 28 = 28°
		const wheel = getByTestId('ego-wheel-left');
		expect(wheel.getAttribute('transform')).toContain('rotate(28)');
	});
});
