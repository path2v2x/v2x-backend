import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import BatteryRange from '$lib/components/dashboard/BatteryRange.svelte';

describe('BatteryRange', () => {
	it('renders default percent and range', () => {
		const { getByTestId } = render(BatteryRange, { props: {} });
		expect(getByTestId('battery-percent').textContent).toContain('82');
		expect(getByTestId('battery-range').textContent).toContain('312');
		expect(getByTestId('battery-range').textContent).toContain('mi');
	});

	it('reflects custom percent value', () => {
		const { getByTestId } = render(BatteryRange, { props: { percent: 45, range: 150 } });
		expect(getByTestId('battery-percent').textContent).toContain('45');
		expect(getByTestId('battery-fill').dataset.fill).toBe('45');
	});

	it('clamps percent above 100', () => {
		const { getByTestId } = render(BatteryRange, { props: { percent: 150 } });
		expect(getByTestId('battery-fill').dataset.fill).toBe('100');
	});

	it('clamps percent below 0', () => {
		const { getByTestId } = render(BatteryRange, { props: { percent: -20 } });
		expect(getByTestId('battery-fill').dataset.fill).toBe('0');
	});

	it('supports km unit', () => {
		const { getByTestId } = render(BatteryRange, {
			props: { percent: 60, range: 250, unit: 'km' },
		});
		expect(getByTestId('battery-range').textContent).toContain('km');
	});
});
