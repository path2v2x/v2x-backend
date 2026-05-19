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
	const VIEW_H = 180;
	const CENTER_X = VIEW_W / 2;
	const CENTER_Y = VIEW_H / 2;
	// Pixels per meter. With radiusM=30, half-viewport (120px wide) covers 30m.
	const PX_PER_M = $derived(Math.min(VIEW_W / 2, VIEW_H / 2) / radiusM);

	// Ego car silhouette (in pixels — drawn always at center).
	const EGO_LEN_M = 4.7; // Model 3-ish
	const EGO_WID_M = 1.85;
	const egoLenPx = $derived(EGO_LEN_M * PX_PER_M);
	const egoWidPx = $derived(EGO_WID_M * PX_PER_M);

	// Front-wheel steer angle. Real cars at full lock are ~30° wheel angle.
	const MAX_WHEEL_DEG = 28;
	const wheelDeg = $derived(steer * MAX_WHEEL_DEG);

	// Wheel sprite dimensions
	const wheelW = $derived(egoWidPx * 0.16);
	const wheelH = $derived(egoLenPx * 0.16);
	const frontY = $derived(-egoLenPx * 0.4);

	/** Project a world point into ego's local frame, then to SVG coords.
	 * Returns null if outside the visible radius. */
	function projectToSvg(
		worldX: number,
		worldY: number
	): { x: number; y: number } | null {
		const dx = worldX - egoPos[0];
		const dy = worldY - egoPos[1];
		const yawRad = (egoYaw * Math.PI) / 180;
		const cos = Math.cos(yawRad);
		const sin = Math.sin(yawRad);
		// World → ego local frame (CARLA left-handed: forward=+x, right=+y)
		const fwd = dx * cos + dy * sin;
		const right = -dx * sin + dy * cos;
		// Beyond radius? hide.
		if (fwd * fwd + right * right > radiusM * radiusM) return null;
		// Ego frame → SVG: ego forward → SVG up (-y), ego right → SVG +x
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
</script>

<svg
	viewBox="0 0 {VIEW_W} {VIEW_H}"
	xmlns="http://www.w3.org/2000/svg"
	class="block w-full h-full"
	style="background: var(--color-tesla-bg);"
	data-testid="vehicle-viz"
	aria-label="Surrounding traffic visualization"
>
	<!-- Subtle radial fade so the edge of the visibility radius softly dims. -->
	<defs>
		<radialGradient id="viz-fade" cx="50%" cy="50%" r="50%">
			<stop offset="0%" stop-color="rgba(255,255,255,0)" />
			<stop offset="85%" stop-color="rgba(255,255,255,0)" />
			<stop offset="100%" stop-color="var(--color-tesla-bg)" />
		</radialGradient>
	</defs>

	<!-- Surrounding traffic (rounded rectangles, rotated into ego frame) -->
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
				rx={egoWidPx * 0.25}
				fill="var(--color-tesla-traffic)"
				opacity="0.85"
			/>
		</g>
	{/each}

	<!-- Soft fade at edges -->
	<rect x="0" y="0" width={VIEW_W} height={VIEW_H} fill="url(#viz-fade)" pointer-events="none" />

	<!-- Ego car (always centered, pointing up). Drawn after traffic so it sits on top. -->
	<g transform="translate({CENTER_X},{CENTER_Y})" data-testid="ego-car">
		<!-- Body -->
		<rect
			x={-egoWidPx / 2}
			y={-egoLenPx / 2}
			width={egoWidPx}
			height={egoLenPx}
			rx={egoWidPx * 0.28}
			fill="var(--color-tesla-vehicle)"
		/>
		<!-- Windshield hint (darker rounded strip near the front) -->
		<rect
			x={-egoWidPx * 0.35}
			y={-egoLenPx * 0.32}
			width={egoWidPx * 0.7}
			height={egoLenPx * 0.16}
			rx={egoWidPx * 0.1}
			fill="rgba(10,10,12,0.55)"
		/>

		<!-- Front wheels — turn with steer. Drawn as small dark rects at the front corners. -->
		<g
			transform="translate({-egoWidPx / 2}, {frontY}) rotate({wheelDeg})"
			data-testid="ego-wheel-left"
		>
			<rect
				x={-wheelW / 2}
				y={-wheelH / 2}
				width={wheelW}
				height={wheelH}
				rx={wheelW * 0.4}
				fill="var(--color-tesla-bg-elevated)"
			/>
		</g>
		<g
			transform="translate({egoWidPx / 2}, {frontY}) rotate({wheelDeg})"
			data-testid="ego-wheel-right"
		>
			<rect
				x={-wheelW / 2}
				y={-wheelH / 2}
				width={wheelW}
				height={wheelH}
				rx={wheelW * 0.4}
				fill="var(--color-tesla-bg-elevated)"
			/>
		</g>
	</g>
</svg>
