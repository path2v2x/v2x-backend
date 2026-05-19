import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import SpeedLimit from '$lib/components/dashboard/SpeedLimit.svelte';

describe('SpeedLimit', () => {
	it('renders a placeholder dash when limit is null/unspecified', () => {
		const { getByTestId } = render(SpeedLimit, { props: {} });
		expect(getByTestId('speed-limit-sign').textContent).toContain('—');
	});

	it('renders the limit value when provided', () => {
		const { getByTestId } = render(SpeedLimit, { props: { limit: 35 } });
		expect(getByTestId('speed-limit-sign').textContent).toContain('35');
	});

	it('shows the unit label', () => {
		const { getByTestId } = render(SpeedLimit, { props: { limit: 50, unit: 'kmh' } });
		expect(getByTestId('speed-limit-sign').textContent).toContain('kmh');
	});
});
