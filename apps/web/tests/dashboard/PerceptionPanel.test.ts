import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import PerceptionPanel from '$lib/components/dashboard/PerceptionPanel.svelte';
import type { Detection } from '$lib/types';

function mkDet(overrides: Partial<Detection> = {}): Detection {
	return {
		id: 'vehicle-0',
		class: 'vehicle',
		pos: [10, 0],
		distance: 10,
		bbox_dim: [4.5, 1.8],
		in_path: true,
		alert: 'none',
		...overrides,
	};
}

function readTranslate(el: SVGGraphicsElement): { x: number; y: number } {
	const t = el.getAttribute('transform') || '';
	const m = t.match(/translate\(([\d.\-]+),([\d.\-]+)\)/);
	if (!m) throw new Error(`No translate in transform: ${t}`);
	return { x: parseFloat(m[1]), y: parseFloat(m[2]) };
}

describe('PerceptionPanel', () => {
	it('always renders the ego car', () => {
		const { getByTestId } = render(PerceptionPanel, { props: { detections: [] } });
		expect(getByTestId('perception-panel')).toBeInTheDocument();
		expect(getByTestId('pp-ego')).toBeInTheDocument();
	});

	it('counts visible detections via data-detections', () => {
		const { getByTestId } = render(PerceptionPanel, {
			props: {
				detections: [
					mkDet({ id: 'a', pos: [5, 0] }),
					mkDet({ id: 'b', pos: [-5, 0] }),
					mkDet({ id: 'c', pos: [0, 5] }),
				],
			},
		});
		expect(getByTestId('perception-panel').dataset.detections).toBe('3');
	});

	it('culls detections beyond the visibility radius', () => {
		const { getByTestId, queryByTestId } = render(PerceptionPanel, {
			props: {
				detections: [
					mkDet({ id: 'near', pos: [10, 0] }),
					mkDet({ id: 'far', pos: [200, 0] }),
				],
				radiusM: 30,
			},
		});
		expect(getByTestId('det-near')).toBeInTheDocument();
		expect(queryByTestId('det-far')).toBeNull();
		expect(getByTestId('perception-panel').dataset.detections).toBe('1');
	});

	it('projects a forward detection above the ego on the SVG (smaller y)', () => {
		const { getByTestId } = render(PerceptionPanel, {
			props: {
				detections: [mkDet({ id: 'ahead', pos: [10, 0] })],
				radiusM: 30,
			},
		});
		// Ego sits at (100, 86.8). A 10m forward detection projects to SVG up (smaller y).
		const t = readTranslate(getByTestId('det-ahead') as unknown as SVGGraphicsElement);
		expect(t.y).toBeLessThan(86.8);
	});

	it('projects a right-side detection to the right of ego (larger x)', () => {
		const { getByTestId } = render(PerceptionPanel, {
			props: {
				detections: [mkDet({ id: 'right', pos: [0, 5] })],
				radiusM: 30,
			},
		});
		const t = readTranslate(getByTestId('det-right') as unknown as SVGGraphicsElement);
		expect(t.x).toBeGreaterThan(100);
	});

	it('uses the right shape per class', () => {
		const classes: Detection['class'][] = [
			'vehicle',
			'pedestrian',
			'cone',
			'traffic_sign',
			'traffic_light',
		];
		const { getByTestId } = render(PerceptionPanel, {
			props: {
				detections: classes.map((c, i) =>
					mkDet({ id: c, class: c, pos: [(i + 1) * 3, 0] })
				),
			},
		});
		// Each class gets its own marker; verify they all render.
		for (const c of classes) {
			expect(getByTestId(`det-${c}`).dataset.class).toBe(c);
		}
	});
});
