<script lang="ts">
	/**
	 * Top-down perception viz — Tesla driver-block style.
	 *
	 * Ego car at the centre, pointing up. Each detection from the bridge's
	 * perception stack is rendered as a type-coloured marker projected
	 * into the ego frame. An animated blue ring expands outward to suggest
	 * active sensor sweep.
	 */

	import type { Detection } from '$lib/types';

	interface Props {
		detections?: Detection[];
		/** Visibility radius in meters (forward + lateral). Detections beyond
		 * are culled. Default 30m fits the dashboard pod size nicely. */
		radiusM?: number;
	}

	let { detections = [], radiusM = 30 }: Props = $props();

	// SVG viewBox — wider than tall so road extends forward.
	const VIEW_W = 200;
	const VIEW_H = 140;
	const CENTER_X = VIEW_W / 2;
	// Bias the ego toward the bottom so most of the panel shows what's
	// ahead — matches how the driver thinks about their surroundings.
	const CENTER_Y = VIEW_H * 0.62;

	const pxPerM = $derived(
		Math.min(VIEW_W / 2, VIEW_H / 2) / radiusM
	);

	// Ego silhouette dimensions (in viewBox units). A Model 3 is ~4.7×1.85m.
	const EGO_LEN_M = 4.7;
	const EGO_WID_M = 1.85;
	const egoLenPx = $derived(EGO_LEN_M * pxPerM);
	const egoWidPx = $derived(EGO_WID_M * pxPerM);

	interface Projected {
		d: Detection;
		x: number;
		y: number;
		w: number;
		h: number;
	}

	function projectOne(d: Detection): Projected | null {
		const [forward, right] = d.pos;
		const distance = Math.hypot(forward, right);
		if (distance > radiusM) return null;
		// Ego frame +forward → SVG up (decreasing y).
		const x = CENTER_X + right * pxPerM;
		const y = CENTER_Y - forward * pxPerM;
		const [lengthM, widthM] = d.bbox_dim;
		return {
			d,
			x,
			y,
			w: Math.max(4, widthM * pxPerM),
			h: Math.max(4, lengthM * pxPerM),
		};
	}

	const projected = $derived(
		detections
			.map(projectOne)
			.filter((p): p is Projected => p !== null)
	);

	// Sensor pulse — same idiom Tesla uses to suggest live perception.
	let pulse = $state(0);
	$effect(() => {
		const start = performance.now();
		let raf = 0;
		const loop = (t: number) => {
			pulse = ((t - start) / 2000) % 1;
			raf = requestAnimationFrame(loop);
		};
		raf = requestAnimationFrame(loop);
		return () => cancelAnimationFrame(raf);
	});
	const pulseRadius = $derived(egoLenPx * 0.5 + pulse * radiusM * pxPerM * 0.95);
	const pulseOpacity = $derived(0.32 * (1 - pulse));
</script>

<svg
	viewBox="0 0 {VIEW_W} {VIEW_H}"
	xmlns="http://www.w3.org/2000/svg"
	preserveAspectRatio="xMidYMid meet"
	class="block w-full h-full"
	data-testid="perception-panel"
	data-detections={projected.length}
	aria-label="Surrounding perception visualization"
>
	<defs>
		<!-- Soft circular fade to the dark edges -->
		<radialGradient id="pp-fade" cx="50%" cy="62%" r="55%">
			<stop offset="0%" stop-color="rgba(255,255,255,0)" />
			<stop offset="75%" stop-color="rgba(8, 9, 12, 0)" />
			<stop offset="100%" stop-color="rgba(8, 9, 12, 0.85)" />
		</radialGradient>

		<linearGradient id="pp-ego" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="#dee0e6" />
			<stop offset="100%" stop-color="#a4aab4" />
		</linearGradient>

		<linearGradient id="pp-vehicle" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="#7d848f" />
			<stop offset="100%" stop-color="#454b56" />
		</linearGradient>

		<filter id="pp-shadow" x="-50%" y="-50%" width="200%" height="200%">
			<feDropShadow dx="0" dy="1.2" stdDeviation="1.4" flood-color="#000" flood-opacity="0.75" />
		</filter>
	</defs>

	<!-- Lane stripes (forward direction only) — gives a sense of road -->
	<g opacity="0.45" stroke="#e8ecf2" stroke-width="0.8" stroke-linecap="round" pointer-events="none">
		<line
			x1={CENTER_X - egoWidPx * 1.1}
			y1="0"
			x2={CENTER_X - egoWidPx * 1.1}
			y2={VIEW_H}
			stroke-dasharray="5 8"
		/>
		<line
			x1={CENTER_X + egoWidPx * 1.1}
			y1="0"
			x2={CENTER_X + egoWidPx * 1.1}
			y2={VIEW_H}
			stroke-dasharray="5 8"
		/>
	</g>

	<!-- Active sensor pulse — outward expanding ring -->
	<circle
		cx={CENTER_X}
		cy={CENTER_Y}
		r={pulseRadius}
		fill="none"
		stroke="#3e82f7"
		stroke-width="1.2"
		opacity={pulseOpacity}
		pointer-events="none"
	/>

	<!-- Detection markers -->
	{#each projected as p (p.d.id)}
		{#if p.d.class === 'vehicle'}
			<g
				transform="translate({p.x},{p.y})"
				filter="url(#pp-shadow)"
				data-testid="det-{p.d.id}"
				data-class={p.d.class}
			>
				<rect
					x={-p.w / 2}
					y={-p.h / 2}
					width={p.w}
					height={p.h}
					rx={p.w * 0.3}
					fill="url(#pp-vehicle)"
					stroke="rgba(0,0,0,0.45)"
					stroke-width="0.5"
				/>
				<rect
					x={-p.w * 0.34}
					y={-p.h * 0.2}
					width={p.w * 0.68}
					height={p.h * 0.5}
					rx={p.w * 0.16}
					fill="rgba(10, 14, 22, 0.65)"
				/>
			</g>
		{:else if p.d.class === 'pedestrian'}
			<g
				transform="translate({p.x},{p.y})"
				data-testid="det-{p.d.id}"
				data-class={p.d.class}
			>
				<circle
					r="3.2"
					fill="#5fc0d6"
					stroke="rgba(0,0,0,0.55)"
					stroke-width="0.5"
					style="filter: drop-shadow(0 0 3px rgba(95, 192, 214, 0.85));"
				/>
				<!-- Small "head" highlight -->
				<circle r="1.4" cy="-0.6" fill="rgba(255,255,255,0.85)" />
			</g>
		{:else if p.d.class === 'cone'}
			<g
				transform="translate({p.x},{p.y})"
				data-testid="det-{p.d.id}"
				data-class={p.d.class}
			>
				<polygon
					points="0,-3.2 2.8,2.8 -2.8,2.8"
					fill="#ff8c00"
					stroke="rgba(0,0,0,0.55)"
					stroke-width="0.5"
					style="filter: drop-shadow(0 0 3px rgba(255, 140, 0, 0.65));"
				/>
				<line x1="-1.8" y1="0.2" x2="1.8" y2="0.2" stroke="rgba(255,255,255,0.85)" stroke-width="0.7" />
			</g>
		{:else if p.d.class === 'traffic_sign'}
			<g
				transform="translate({p.x},{p.y})"
				data-testid="det-{p.d.id}"
				data-class={p.d.class}
			>
				<rect
					x="-2.6"
					y="-2.6"
					width="5.2"
					height="5.2"
					rx="1"
					fill="#ffffff"
					stroke="rgba(0,0,0,0.6)"
					stroke-width="0.5"
					style="filter: drop-shadow(0 0 3px rgba(255, 255, 255, 0.55));"
				/>
			</g>
		{:else}
			<g
				transform="translate({p.x},{p.y})"
				data-testid="det-{p.d.id}"
				data-class={p.d.class}
			>
				<!-- traffic_light: vertical pill with three lamps (real Tesla
				     shows actual light state; we don't have that — render a
				     neutral indicator and let the warning stack carry state). -->
				<rect x="-1.8" y="-4.2" width="3.6" height="8.4" rx="1.4" fill="#1d2128" stroke="rgba(255,255,255,0.35)" stroke-width="0.4" />
				<circle cx="0" cy="-2.5" r="0.9" fill="#ff5a5a" opacity="0.55" />
				<circle cx="0" cy="0"    r="0.9" fill="#ffd24a" opacity="0.55" />
				<circle cx="0" cy="2.5"  r="0.9" fill="#5fd66f" opacity="0.55" />
			</g>
		{/if}
	{/each}

	<!-- Edge fade -->
	<rect x="0" y="0" width={VIEW_W} height={VIEW_H} fill="url(#pp-fade)" pointer-events="none" />

	<!-- Ego car at centre, pointing forward (up). -->
	<g
		transform="translate({CENTER_X},{CENTER_Y})"
		filter="url(#pp-shadow)"
		data-testid="pp-ego"
	>
		<rect
			x={-egoWidPx / 2}
			y={-egoLenPx / 2}
			width={egoWidPx}
			height={egoLenPx}
			rx={egoWidPx * 0.32}
			fill="url(#pp-ego)"
			stroke="rgba(255,255,255,0.18)"
			stroke-width="0.5"
		/>
		<!-- Windshield -->
		<path
			d="M {-egoWidPx * 0.40} {-egoLenPx * 0.22}
			   L {-egoWidPx * 0.34} {-egoLenPx * 0.05}
			   L {egoWidPx * 0.34} {-egoLenPx * 0.05}
			   L {egoWidPx * 0.40} {-egoLenPx * 0.22}
			   Z"
			fill="rgba(20, 28, 40, 0.7)"
		/>
		<!-- Headlights -->
		<rect
			x={-egoWidPx * 0.42}
			y={-egoLenPx * 0.48}
			width={egoWidPx * 0.16}
			height={egoLenPx * 0.045}
			rx="0.6"
			fill="#fff6c2"
			style="filter: drop-shadow(0 0 3px rgba(255, 246, 194, 0.95));"
		/>
		<rect
			x={egoWidPx * 0.26}
			y={-egoLenPx * 0.48}
			width={egoWidPx * 0.16}
			height={egoLenPx * 0.045}
			rx="0.6"
			fill="#fff6c2"
			style="filter: drop-shadow(0 0 3px rgba(255, 246, 194, 0.95));"
		/>
	</g>
</svg>
