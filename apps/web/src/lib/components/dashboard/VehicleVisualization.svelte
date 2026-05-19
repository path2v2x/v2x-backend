<script lang="ts">
	/**
	 * Top-down stylized vehicle visualization (Tesla driver-block aesthetic).
	 *
	 * Ego car is always centered on the SVG pointing up. Lane stripes flank
	 * the ego, soft headlight beams project forward, surrounding traffic is
	 * projected into the ego's local frame. Front wheels rotate with steer.
	 */

	export interface NearbyActor {
		id: number;
		pos: [number, number]; // world (x, y) in meters
		yaw: number; // degrees
		type?: string;
	}

	interface Props {
		egoPos: [number, number];
		egoYaw: number;
		steer: number;
		nearby?: NearbyActor[];
		radiusM?: number;
		/** Show headlight beams (typically when moving forward or low light). */
		showHeadlights?: boolean;
	}

	let {
		egoPos = [0, 0],
		egoYaw = 0,
		steer = 0,
		nearby = [],
		radiusM = 30,
		showHeadlights = true,
	}: Props = $props();

	const VIEW_W = 260;
	const VIEW_H = 220;
	const CENTER_X = VIEW_W / 2;
	const CENTER_Y = VIEW_H * 0.58; // bias ego slightly low so more road shows ahead
	const PX_PER_M = $derived(Math.min(VIEW_W / 2, VIEW_H / 2) / radiusM);

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

	// Sensor pulse animation
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
	const pulseOpacity = $derived(0.22 * (1 - pulse));

	// Lane geometry: two stripes flanking the ego at ~1.6m offset (typical lane half-width).
	const laneOffsetPx = $derived(EGO_WID_M * 1.05 * PX_PER_M);
</script>

<svg
	viewBox="0 0 {VIEW_W} {VIEW_H}"
	xmlns="http://www.w3.org/2000/svg"
	class="block w-full h-full"
	data-testid="vehicle-viz"
	aria-label="Surrounding traffic visualization"
>
	<defs>
		<!-- Background: deep blue-black with a warmer road tint near ego -->
		<radialGradient id="viz-bg" cx="50%" cy="58%" r="60%">
			<stop offset="0%" stop-color="#1c2230" />
			<stop offset="60%" stop-color="#0e1015" />
			<stop offset="100%" stop-color="#050608" />
		</radialGradient>

		<!-- Ego car body gradient -->
		<linearGradient id="ego-body" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="#dde0e6" />
			<stop offset="100%" stop-color="#a8aeb8" />
		</linearGradient>

		<!-- Glass gradient (cool dark, subtle gloss) -->
		<linearGradient id="ego-glass" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="rgba(90, 115, 150, 0.85)" />
			<stop offset="50%" stop-color="rgba(20, 28, 40, 0.92)" />
			<stop offset="100%" stop-color="rgba(60, 80, 110, 0.78)" />
		</linearGradient>

		<!-- Traffic vehicle gradient -->
		<linearGradient id="traffic-body" x1="0%" y1="0%" x2="0%" y2="100%">
			<stop offset="0%" stop-color="#737a86" />
			<stop offset="100%" stop-color="#3f454f" />
		</linearGradient>

		<!-- Drop-shadow filter for ego -->
		<filter id="ego-shadow" x="-50%" y="-50%" width="200%" height="200%">
			<feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#000" flood-opacity="0.75" />
		</filter>

		<!-- Edge fade -->
		<radialGradient id="viz-fade" cx="50%" cy="58%" r="55%">
			<stop offset="0%" stop-color="rgba(255,255,255,0)" />
			<stop offset="80%" stop-color="rgba(5, 6, 8, 0.0)" />
			<stop offset="100%" stop-color="rgba(5, 6, 8, 0.95)" />
		</radialGradient>

		<!-- Headlight beam cone gradient -->
		<radialGradient id="headlight-beam" cx="50%" cy="100%" r="100%">
			<stop offset="0%" stop-color="rgba(255, 246, 200, 0.45)" />
			<stop offset="55%" stop-color="rgba(255, 246, 200, 0.12)" />
			<stop offset="100%" stop-color="rgba(255, 246, 200, 0)" />
		</radialGradient>
	</defs>

	<!-- Background -->
	<rect width={VIEW_W} height={VIEW_H} fill="url(#viz-bg)" />

	<!-- Lane stripes (white dashed, parallel to ego forward) -->
	<g opacity="0.65" stroke="#e8ecf2" stroke-width="2" stroke-linecap="round" pointer-events="none">
		<line
			x1={CENTER_X - laneOffsetPx}
			y1="0"
			x2={CENTER_X - laneOffsetPx}
			y2={VIEW_H}
			stroke-dasharray="10 14"
		/>
		<line
			x1={CENTER_X + laneOffsetPx}
			y1="0"
			x2={CENTER_X + laneOffsetPx}
			y2={VIEW_H}
			stroke-dasharray="10 14"
		/>
	</g>

	<!-- Outer faint road grid (suggests wider road) -->
	<g opacity="0.18" stroke="#ffffff" stroke-width="0.6" pointer-events="none">
		<line
			x1={CENTER_X - laneOffsetPx * 2.2}
			y1="0"
			x2={CENTER_X - laneOffsetPx * 2.2}
			y2={VIEW_H}
			stroke-dasharray="3 9"
		/>
		<line
			x1={CENTER_X + laneOffsetPx * 2.2}
			y1="0"
			x2={CENTER_X + laneOffsetPx * 2.2}
			y2={VIEW_H}
			stroke-dasharray="3 9"
		/>
	</g>

	<!-- Headlight beams (projected forward from the front bumper) -->
	{#if showHeadlights}
		<g transform="translate({CENTER_X},{CENTER_Y}) rotate({wheelDeg * 0.3})">
			<!-- Left beam -->
			<ellipse
				cx={-egoWidPx * 0.34}
				cy={-egoLenPx * 1.0}
				rx={egoWidPx * 0.7}
				ry={egoLenPx * 0.95}
				fill="url(#headlight-beam)"
				opacity="0.9"
			/>
			<!-- Right beam -->
			<ellipse
				cx={egoWidPx * 0.34}
				cy={-egoLenPx * 1.0}
				rx={egoWidPx * 0.7}
				ry={egoLenPx * 0.95}
				fill="url(#headlight-beam)"
				opacity="0.9"
			/>
		</g>
	{/if}

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
			stroke="rgba(255,255,255,0.14)"
			stroke-width="0.6"
		/>

		<!-- Hood line -->
		<path
			d="M {-egoWidPx * 0.46} {-egoLenPx * 0.22}
				Q 0 {-egoLenPx * 0.30}
				{egoWidPx * 0.46} {-egoLenPx * 0.22}"
			fill="none"
			stroke="rgba(0,0,0,0.28)"
			stroke-width="0.6"
		/>

		<!-- Windshield -->
		<path
			d="M {-egoWidPx * 0.40} {-egoLenPx * 0.22}
				L {-egoWidPx * 0.34} {-egoLenPx * 0.04}
				L {egoWidPx * 0.34} {-egoLenPx * 0.04}
				L {egoWidPx * 0.40} {-egoLenPx * 0.22}
				Z"
			fill="url(#ego-glass)"
		/>

		<!-- Roof -->
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

		<!-- Headlight housings -->
		<rect
			x={-egoWidPx * 0.42}
			y={-egoLenPx * 0.48}
			width={egoWidPx * 0.16}
			height={egoLenPx * 0.045}
			rx="1"
			fill="#fff6c2"
			style="filter: drop-shadow(0 0 5px rgba(255, 246, 194, 0.95));"
		/>
		<rect
			x={egoWidPx * 0.26}
			y={-egoLenPx * 0.48}
			width={egoWidPx * 0.16}
			height={egoLenPx * 0.045}
			rx="1"
			fill="#fff6c2"
			style="filter: drop-shadow(0 0 5px rgba(255, 246, 194, 0.95));"
		/>

		<!-- Taillight LED strip -->
		<rect
			x={-egoWidPx * 0.42}
			y={egoLenPx * 0.44}
			width={egoWidPx * 0.84}
			height={egoLenPx * 0.04}
			rx="1.2"
			fill="#ff3030"
			opacity="0.85"
			style="filter: drop-shadow(0 0 3px rgba(255, 48, 48, 0.75));"
		/>

		<!-- Front wheels -->
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
