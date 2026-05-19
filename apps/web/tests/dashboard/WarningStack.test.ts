import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import WarningStack, {
	type DashboardWarning,
} from '$lib/components/dashboard/WarningStack.svelte';

function mkWarning(overrides: Partial<DashboardWarning> = {}): DashboardWarning {
	return {
		id: 'w1',
		message: 'Test warning',
		severity: 'warning',
		source: 'eva',
		lastUpdate: 1000,
		...overrides,
	};
}

describe('WarningStack', () => {
	it('renders nothing visible when no warnings provided', () => {
		const { getByTestId, queryByTestId } = render(WarningStack, {
			props: { warnings: [], now: 1000 },
		});
		expect(getByTestId('warning-empty')).toBeInTheDocument();
		expect(queryByTestId('warning-overflow')).toBeNull();
	});

	it('renders a fresh warning', () => {
		const { getByTestId } = render(WarningStack, {
			props: {
				warnings: [mkWarning({ id: 'w1', message: 'Pull over', lastUpdate: 1000 })],
				now: 1500,
			},
		});
		expect(getByTestId('warning-w1')).toBeInTheDocument();
		expect(getByTestId('warning-msg-w1').textContent).toContain('Pull over');
	});

	it('filters out stale warnings beyond fadeMs', () => {
		const { queryByTestId } = render(WarningStack, {
			props: {
				warnings: [mkWarning({ id: 'old', lastUpdate: 1000 })],
				now: 5000, // 4000 ms later, well past default 1500ms fade
				fadeMs: 1500,
			},
		});
		expect(queryByTestId('warning-old')).toBeNull();
	});

	it('shows secondary detail when provided', () => {
		const { getByTestId } = render(WarningStack, {
			props: {
				warnings: [mkWarning({ id: 'eva1', detail: '12.4m' })],
				now: 1000,
			},
		});
		expect(getByTestId('warning-detail-eva1').textContent).toBe('12.4m');
	});

	it('sorts newest warnings first', () => {
		const { container } = render(WarningStack, {
			props: {
				warnings: [
					mkWarning({ id: 'old', lastUpdate: 100, message: 'older' }),
					mkWarning({ id: 'new', lastUpdate: 900, message: 'newer' }),
				],
				now: 1000,
			},
		});
		const items = container.querySelectorAll('[data-testid^="warning-"][data-severity]');
		expect(items.length).toBe(2);
		expect((items[0] as HTMLElement).dataset.testid).toBe('warning-new');
		expect((items[1] as HTMLElement).dataset.testid).toBe('warning-old');
	});

	it('caps to maxVisible and shows overflow badge', () => {
		const warnings = Array.from({ length: 6 }, (_, i) =>
			mkWarning({ id: `w${i}`, lastUpdate: 1000 - i })
		);
		const { container, getByTestId } = render(WarningStack, {
			props: { warnings, now: 1000, maxVisible: 3 },
		});
		const visible = container.querySelectorAll('[data-testid^="warning-w"]');
		expect(visible.length).toBe(3);
		expect(getByTestId('warning-overflow').textContent).toContain('+3 more');
	});

	it('does not show overflow when count <= maxVisible', () => {
		const warnings = Array.from({ length: 3 }, (_, i) =>
			mkWarning({ id: `w${i}`, lastUpdate: 1000 - i })
		);
		const { queryByTestId } = render(WarningStack, {
			props: { warnings, now: 1000, maxVisible: 4 },
		});
		expect(queryByTestId('warning-overflow')).toBeNull();
	});

	it('applies severity attribute for styling hooks', () => {
		const { getByTestId } = render(WarningStack, {
			props: {
				warnings: [
					mkWarning({ id: 'c', severity: 'critical' }),
					mkWarning({ id: 'w', severity: 'warning', lastUpdate: 999 }),
					mkWarning({ id: 'i', severity: 'info', lastUpdate: 998 }),
				],
				now: 1000,
			},
		});
		expect(getByTestId('warning-c').dataset.severity).toBe('critical');
		expect(getByTestId('warning-w').dataset.severity).toBe('warning');
		expect(getByTestId('warning-i').dataset.severity).toBe('info');
	});

	it('exposes data-count on the container for parent observability', () => {
		const { getByTestId } = render(WarningStack, {
			props: {
				warnings: [mkWarning({ id: 'a' }), mkWarning({ id: 'b', lastUpdate: 1000 })],
				now: 1000,
			},
		});
		expect(getByTestId('warning-stack').dataset.count).toBe('2');
	});
});
