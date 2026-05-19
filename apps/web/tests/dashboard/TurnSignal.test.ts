import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import TurnSignal from '$lib/components/dashboard/TurnSignal.svelte';

describe('TurnSignal', () => {
	it('renders both chevrons inactive when direction is null', () => {
		const { getByTestId } = render(TurnSignal, { props: { direction: null } });
		expect(getByTestId('turn-signal-left').dataset.active).toBe('false');
		expect(getByTestId('turn-signal-right').dataset.active).toBe('false');
	});

	it('marks left active when direction is left', () => {
		const { getByTestId } = render(TurnSignal, { props: { direction: 'left' } });
		expect(getByTestId('turn-signal-left').dataset.active).toBe('true');
		expect(getByTestId('turn-signal-right').dataset.active).toBe('false');
	});

	it('marks right active when direction is right', () => {
		const { getByTestId } = render(TurnSignal, { props: { direction: 'right' } });
		expect(getByTestId('turn-signal-right').dataset.active).toBe('true');
		expect(getByTestId('turn-signal-left').dataset.active).toBe('false');
	});
});
