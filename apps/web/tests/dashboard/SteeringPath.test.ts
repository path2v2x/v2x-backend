import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import SteeringPath from '$lib/components/dashboard/SteeringPath.svelte';

describe('SteeringPath', () => {
	it('renders the ego car and both guide lines', () => {
		const { getByTestId } = render(SteeringPath, { props: { steer: 0 } });
		expect(getByTestId('steering-path')).toBeInTheDocument();
		expect(getByTestId('path-ego-car')).toBeInTheDocument();
		expect(getByTestId('path-guide-left')).toBeInTheDocument();
		expect(getByTestId('path-guide-right')).toBeInTheDocument();
	});

	it('exposes clamped steer in data-steer (zero stays at center)', () => {
		const { getByTestId } = render(SteeringPath, { props: { steer: 0 } });
		expect(getByTestId('steering-path').dataset.steer).toBe('0.000');
	});

	it('clamps steer below -1', () => {
		const { getByTestId } = render(SteeringPath, { props: { steer: -2 } });
		expect(getByTestId('steering-path').dataset.steer).toBe('-1.000');
	});

	it('clamps steer above +1', () => {
		const { getByTestId } = render(SteeringPath, { props: { steer: 5 } });
		expect(getByTestId('steering-path').dataset.steer).toBe('1.000');
	});

	function endX(path: SVGPathElement): number {
		// "M sx,sy Q cx,cy ex,ey" — pull ex
		const d = path.getAttribute('d') || '';
		const m = d.match(/Q\s*[\d.\-]+,[\d.\-]+\s+([\d.\-]+),/);
		return m ? parseFloat(m[1]) : NaN;
	}

	it('curves the guides to the right when steer is positive', () => {
		const { getByTestId } = render(SteeringPath, { props: { steer: 0.6 } });
		const left = getByTestId('path-guide-left') as unknown as SVGPathElement;
		// At steer=0 the left guide ends at x = 50 - 9 = 41. With +0.6 steer
		// the end shifts right by 0.6 * 36 = +21.6 → 62.6.
		expect(endX(left)).toBeGreaterThan(50);
	});

	it('curves the guides to the left when steer is negative', () => {
		const { getByTestId } = render(SteeringPath, { props: { steer: -0.6 } });
		const right = getByTestId('path-guide-right') as unknown as SVGPathElement;
		// At steer=0 the right guide ends at x = 50 + 9 = 59. With -0.6 steer
		// the end shifts left by -0.6 * 36 = -21.6 → 37.4.
		expect(endX(right)).toBeLessThan(50);
	});

	it('keeps the guides straight at zero steer (left end < right end)', () => {
		const { getByTestId } = render(SteeringPath, { props: { steer: 0 } });
		const left = getByTestId('path-guide-left') as unknown as SVGPathElement;
		const right = getByTestId('path-guide-right') as unknown as SVGPathElement;
		expect(endX(left)).toBeLessThan(endX(right));
	});
});
