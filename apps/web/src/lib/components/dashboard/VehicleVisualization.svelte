<script lang="ts">
	/**
	 * Top-down stylized vehicle visualization, Tesla driver-block style.
	 *
	 * Ego car is always centered on the SVG pointing up. Nearby actors
	 * are projected into the ego's local frame and drawn as rounded
	 * rectangles. Ego's front wheels turn with the `steer` input.
	 */

	export interface NearbyActor {
		id: number;
		pos: [number, number]; // world (x, y) in meters
		yaw: number; // degrees
		type?: string;
	}

	interface Props {
		/** Ego position in world coords, [x, y] meters. */
		egoPos: [number, number];
		/** Ego yaw in degrees (CARLA convention). */
		egoYaw: number;
		/** Steering input in [-1, 1]. Drives front-wheel rotation. */
		steer: number;
		/** Other vehicles within range, from bridge telemetry. */
		nearby?: NearbyActor[];
		/** Visibility radius in meters. */
		radiusM?: number;
	}

	let {
		egoPos = [0, 0],
		egoYaw = 0,
		steer = 0,
		nearby = [],
		radiusM = 30,
	}: Props = $props();

	// SVG viewport
	const VIEW_W = 240;
	const VIEW_H = 200;
	const CENTER_X = VIEW_W / 2;
	const CENTER_Y = VIEW_H / 2;
	const PX_PER_M = $derived(Math.min(VIEW_W / 2, VIEW_H / 2) / radiusM);

	// Ego car silhouette (Tesla Model 3-ish proportions)
	const EGO_LEN_M = 4.7;
	const EGO_WID_M = 1.85;
	const egoLenPx = $derived(EGO_LEN_M * PX_PER_M);
	const egoWidPx = $derived(EGO_WID_M * PX_PER_M);

	const MAX_WHEEL_DEG = 28;
	const wheelDeg = $derived(steer * MAX_WHEEL_DEG);

	const wheelW = $derived(egoWidPx * 0.14);
	const wheelH = $derived(egoLenPx * 0.14);
	const frontY = $derived(-egoLenPx * 0.36);
	const rearY = $derived(egoLenPx * 0.36);

	function projectToSvg(
		worldX: number,
		worldY: number
	): { x: number; y: number } | null {
		const dx = worldX - egoPos[0];
		const dy = worldY - egoPos[1];
		const yawRad = (egoYaw * Math.PI) / 180;
		const cos = Math.cos(yawRad);
		const sin = Math.sin(yawRad);
		const fwd = dx * cos + dy * sin;
		const right = -dx * sin + dy * cos;
		if (fwd * fwd + right * right > radiusM * radiusM) return null;
		return {
			x: CENTER_X + right * PX_PER_M,
			y: CENTER_Y - fwd * PX_PER_M,
		};
	}

	interface ProjectedActor {
		id: number;
		x: number;
		y: number;
		relYaw: number;
	}

	const projected = $derived.by<ProjectedActor[]>(() => {
		const result: ProjectedActor[] = [];
		for (const a of nearby) {
			const p = projectToSvg(a.pos[0], a.pos[1]);
			if (p == null) continue;
			result.push({ id: a.id, x: p.x, y: p.y, relYaw: a.yaw - egoYaw });
		}
		return result;
	});

	// Subtle "sensor pulse" animation around the ego — Tesla active-perception accent.
	let pulse = $state(0);
	$effect(() => {
		const start = performance.now();
		let raf = 0;
		function loop(t: number) {
			pulse = ((t - start) / 1800) % 1;
			raf = requestAnimationFrame(loop);
		}
		raf = requestAnimationFrame(loop);
		return () => cancelAnimationFrame(raf);
	});
	const pulseRadius = $derived(egoLenPx * 0.6 + pulse * egoLenPx * 1.8);
	const pulseOpacity = $derived(0.25 * (1 - pulse));
</script>

<svg
	viewBox="0 0 {VIEW_W} {VIEW_H}"
	xmlns="http://www.w3.org/2000/svg"
	class="block w-full h-full"
	data-testid="vehicle-viz"
	aria-label="Surrounding traffic visualization"
>
	<defs>
		<!-- Subtle background gradient: top darker, bottom slightly lighter -->
		<linearGradient id="viz-bg" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="#0a0a0c" />
			<stop offset="100%" stop-color="#14171c" />
		</linearGradient>

		<!-- Ego car body gradient (light grey, slightly warmer at front) -->
		<linearGradient id="ego-body" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="#d8dbe0" />
			<stop offset="100%" stop-color="#a8aeb8" />
		</linearGradient>

		<!-- Windshield gradient (cool dark with subtle gloss) -->
		<linearGradient id="ego-glass" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="rgba(80, 100, 130, 0.85)" />
			<stop offset="50%" stop-color="rgba(20, 28, 40, 0.92)" />
			<stop offset="100%" stop-color="rgba(50, 70, 100, 0.78)" />
		</linearGradient>

		<!-- Drop-shadow filter for the ego car -->
		<filter id="ego-shadow" x="-50%" y="-50%" width="200%" height="200%">
			<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#000" flood-opacity="0.7" />
		</filter>

		<!-- Traffic vehicle gradient -->
		<linearGradient id="traffic-body" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="#6a7280" />
			<stop offset="100%" stop-color="#454c57" />
		</linearGradient>

		<!-- Soft radial fade so the edge of visibility softly dims -->
		<radialGradient id="viz-fade" cx="50%" cy="50%" r="50%">
			<stop offset="0%" stop-color="rgba(255,255,255,0)" />
			<stop offset="80%" stop-color="rgba(10,10,12,0.0)" />
			<stop offset="100%" stop-color="rgba(10,10,12,0.95)" />
		</radialGradient>
	</defs>

	<!-- Background -->
	<rect width={VIEW_W} height={VIEW_H} fill="url(#viz-bg)" />

	<!-- Faint road grid lines (parallel to ego's forward direction) -->
	<g opacity="0.08" stroke="#ffffff" stroke-width="0.5">
		{#each [-2, -1, 0, 1, 2] as i}
			<line
				x1={CENTER_X + i * egoWidPx * 1.2}
				y1="0"
				x2={CENTER_X + i * egoWidPx * 1.2}
				y2={VIEW_H}
				stroke-dasharray="4 6"
			/>
		{/each}
	</g>

	<!-- Sensor pulse ring (Tesla active-perception accent) -->
	<circle
		cx={CENTER_X}
		cy={CENTER_Y}
		r={pulseRadius}
		fill="none"
		stroke="#3e82f7"
		stroke-width="1.4"
		opacity={pulseOpacity}
		pointer-events="none"
	/>

	<!-- Surrounding traffic -->
	{#each projected as a (a.id)}
		<g
			transform="translate({a.x},{a.y}) rotate({-a.relYaw})"
			data-testid="nearby-{a.id}"
		>
			<!-- Body -->
			<rect
				x={-egoWidPx / 2}
				y={-egoLenPx / 2}
				width={egoWidPx}
				height={egoLenPx}
				rx={egoWidPx * 0.28}
				fill="url(#traffic-body)"
				stroke="rgba(0,0,0,0.4)"
				stroke-width="0.6"
				opacity="0.92"
			/>
			<!-- Roof hint -->
			<rect
				x={-egoWidPx * 0.34}
				y={-egoLenPx * 0.18}
				width={egoWidPx * 0.68}
				height={egoLenPx * 0.45}
				rx={egoWidPx * 0.18}
				fill="rgba(10, 14, 22, 0.55)"
			/>
		</g>
	{/each}

	<!-- Edge fade -->
	<rect x="0" y="0" width={VIEW_W} height={VIEW_H} fill="url(#viz-fade)" pointer-events="none" />

	<!-- Ego car -->
	<g transform="translate({CENTER_X},{CENTER_Y})" data-testid="ego-car" filter="url(#ego-shadow)">
		<!-- Body -->
		<rect
			x={-egoWidPx / 2}
			y={-egoLenPx / 2}
			width={egoWidPx}
			height={egoLenPx}
			rx={egoWidPx * 0.32}
			fill="url(#ego-body)"
			stroke="rgba(255,255,255,0.12)"
			stroke-width="0.6"
		/>

		<!-- Hood/cowl line -->
		<path
			d="M {-egoWidPx * 0.46} {-egoLenPx * 0.22}
				Q 0 {-egoLenPx * 0.30}
				{egoWidPx * 0.46} {-egoLenPx * 0.22}"
			fill="none"
			stroke="rgba(0,0,0,0.25)"
			stroke-width="0.6"
		/>

		<!-- Windshield (front glass) -->
		<path
			d="M {-egoWidPx * 0.40} {-egoLenPx * 0.22}
				L {-egoWidPx * 0.34} {-egoLenPx * 0.04}
				L {egoWidPx * 0.34} {-egoLenPx * 0.04}
				L {egoWidPx * 0.40} {-egoLenPx * 0.22}
				Z"
			fill="url(#ego-glass)"
		/>

		<!-- Roof (slightly darker than body for depth) -->
		<rect
			x={-egoWidPx * 0.36}
			y={-egoLenPx * 0.04}
			width={egoWidPx * 0.72}
			height={egoLenPx * 0.30}
			rx={egoWidPx * 0.16}
			fill="rgba(180, 188, 200, 0.92)"
		/>

		<!-- Rear glass -->
		<path
			d="M {-egoWidPx * 0.34} {egoLenPx * 0.26}
				L {-egoWidPx * 0.40} {egoLenPx * 0.40}
				L {egoWidPx * 0.40} {egoLenPx * 0.40}
				L {egoWidPx * 0.34} {egoLenPx * 0.26}
				Z"
			fill="url(#ego-glass)"
			opacity="0.85"
		/>

		<!-- Headlights (front) -->
		<rect
			x={-egoWidPx * 0.42}
			y={-egoLenPx * 0.48}
			width={egoWidPx * 0.16}
			height={egoLenPx * 0.045}
			rx="1"
			fill="#fff6c2"
			style="filter: drop-shadow(0 0 4px rgba(255, 246, 194, 0.9));"
		/>
		<rect
			x={egoWidPx * 0.26}
			y={-egoLenPx * 0.48}
			width={egoWidPx * 0.16}
			height={egoLenPx * 0.045}
			rx="1"
			fill="#fff6c2"
			style="filter: drop-shadow(0 0 4px rgba(255, 246, 194, 0.9));"
		/>

		<!-- Taillights (rear, single LED strip across the back) -->
		<rect
			x={-egoWidPx * 0.42}
			y={egoLenPx * 0.44}
			width={egoWidPx * 0.84}
			height={egoLenPx * 0.04}
			rx="1.2"
			fill="#ff3030"
			opacity="0.85"
			style="filter: drop-shadow(0 0 3px rgba(255, 48, 48, 0.7));"
		/>

		<!-- Front wheels — turn with steer -->
		<g transform="translate({-egoWidPx / 2}, {frontY}) rotate({wheelDeg})" data-testid="ego-wheel-left">
			<rect
				x={-wheelW / 2}
				y={-wheelH / 2}
				width={wheelW}
				height={wheelH}
				rx={wheelW * 0.35}
				fill="#15181d"
				stroke="rgba(0,0,0,0.4)"
				stroke-width="0.4"
			/>
		</g>
		<g transform="translate({egoWidPx / 2}, {frontY}) rotate({wheelDeg})" data-testid="ego-wheel-right">
			<rect
				x={-wheelW / 2}
				y={-wheelH / 2}
				width={wheelW}
				height={wheelH}
				rx={wheelW * 0.35}
				fill="#15181d"
				stroke="rgba(0,0,0,0.4)"
				stroke-width="0.4"
			/>
		</g>

		<!-- Rear wheels (fixed) -->
		<g transform="translate({-egoWidPx / 2}, {rearY})">
			<rect
				x={-wheelW / 2}
				y={-wheelH / 2}
				width={wheelW}
				height={wheelH}
				rx={wheelW * 0.35}
				fill="#15181d"
				stroke="rgba(0,0,0,0.4)"
				stroke-width="0.4"
			/>
		</g>
		<g transform="translate({egoWidPx / 2}, {rearY})">
			<rect
				x={-wheelW / 2}
				y={-wheelH / 2}
				width={wheelW}
				height={wheelH}
				rx={wheelW * 0.35}
				fill="#15181d"
				stroke="rgba(0,0,0,0.4)"
				stroke-width="0.4"
			/>
		</g>
	</g>
</svg>
